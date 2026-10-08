/*
 * xg_touch.m: a touch gamepad for the Xbox engine, laid out like the Xbox
 * controller Halo was made for. It drives player 1's gamepad (xg_ios.m):
 *
 * - left side: a floating stick that appears where the thumb lands (move);
 * - right side: drag to look (the right stick follows the drag's speed);
 * - buttons: RT fire, LT grenade, A jump, B melee, X reload/action, Y weapon,
 *   RB grenade type, LB flashlight, crouch and zoom (the stick clicks),
 *   Start and Back.
 *
 * It hides itself while a game controller is connected.
 */
#import <UIKit/UIKit.h>
#include <SDL3/SDL_gamepad.h>
#include <math.h>
#include "xg_ios.h"

#define STICK_RADIUS 70.0
#define LOOK_SPEED 900.0 /* points per second for a full right stick at 1x */
#define LOOK_SENSITIVITY_KEY @"XGTouchLookSensitivity"
#define LOOK_SENSITIVITY_MIN 0.1f
#define LOOK_SENSITIVITY_MAX 8.0f
#define OVERLAY_OFF_KEY @"XGTouchOverlayOff"

struct touch_button
{
	const char *label;
	int button;          /* SDL_GamepadButton, or -1/-2 for the left/right trigger */
	CGFloat x, y, size;  /* from the right edge and the bottom edge, in points */
};

static const struct touch_button buttons[] =
{
	{ "RT", -2, 100, 250, 74 },
	{ "LT", -1, 190, 300, 56 },
	{ "A", SDL_GAMEPAD_BUTTON_SOUTH, 90, 70, 64 },
	{ "B", SDL_GAMEPAD_BUTTON_EAST, 30, 140, 56 },
	{ "X", SDL_GAMEPAD_BUTTON_WEST, 170, 110, 56 },
	{ "Y", SDL_GAMEPAD_BUTTON_NORTH, 100, 170, 52 },
	{ "RB", SDL_GAMEPAD_BUTTON_RIGHT_SHOULDER, 30, 250, 48 },
	{ "LB", SDL_GAMEPAD_BUTTON_LEFT_SHOULDER, 260, 250, 44 },
	{ "Crouch", SDL_GAMEPAD_BUTTON_LEFT_STICK, 250, 50, 52 },
	{ "Zoom", SDL_GAMEPAD_BUTTON_RIGHT_STICK, 250, 150, 48 },
};

@implementation XGTouchPad
{
	UITouch *stick_touch, *look_touch;
	CGPoint stick_center, stick_point, look_last;
	CFTimeInterval look_time;
	CGPoint look_velocity;
	NSMapTable<UITouch *, NSNumber *> *button_touches;
	NSArray<UIView *> *button_views;
	UIView *stick_base, *stick_knob;
	UIButton *start_button, *back_button;
	UIButton *sensitivity_button;
	UIView *sensitivity_panel;
	UISlider *sensitivity_slider;
	UILabel *sensitivity_label;
	float look_sensitivity;
	UIButton *hide_button, *show_button;  /* the overlay off, and the faint button that brings it back */
	BOOL overlay_off;
	struct xg_touch_pad state;
	CADisplayLink *link;
	BOOL inactive;
}

- (instancetype)initWithFrame:(CGRect)frame
{
	NSMutableArray *views = [NSMutableArray array];
	self = [super initWithFrame:frame];
	self.multipleTouchEnabled = YES;
	self.backgroundColor = UIColor.clearColor;
	button_touches = [NSMapTable strongToStrongObjectsMapTable];
	for (size_t index = 0; index < sizeof(buttons) / sizeof(buttons[0]); index++)
	{
		UILabel *view = [UILabel new];
		view.text = @(buttons[index].label);
		view.textAlignment = NSTextAlignmentCenter;
		view.font = [UIFont systemFontOfSize:buttons[index].size > 60 ? 20 : 15 weight:UIFontWeightSemibold];
		view.textColor = [UIColor colorWithWhite:1 alpha:0.85];
		view.backgroundColor = [UIColor colorWithWhite:0 alpha:0.28];
		view.layer.borderColor = [UIColor colorWithWhite:1 alpha:0.45].CGColor;
		view.layer.borderWidth = 1.5;
		view.layer.cornerRadius = buttons[index].size / 2;
		view.clipsToBounds = YES;
		view.userInteractionEnabled = NO;
		[self addSubview:view];
		[views addObject:view];
	}
	button_views = views;
	stick_base = [UIView new];
	stick_base.frame = CGRectMake(0, 0, STICK_RADIUS * 2, STICK_RADIUS * 2);
	stick_base.layer.cornerRadius = STICK_RADIUS;
	stick_base.layer.borderColor = [UIColor colorWithWhite:1 alpha:0.4].CGColor;
	stick_base.layer.borderWidth = 1.5;
	stick_base.backgroundColor = [UIColor colorWithWhite:0 alpha:0.2];
	stick_base.userInteractionEnabled = NO;
	stick_base.hidden = YES;
	stick_knob = [UIView new];
	stick_knob.frame = CGRectMake(0, 0, 56, 56);
	stick_knob.layer.cornerRadius = 28;
	stick_knob.backgroundColor = [UIColor colorWithWhite:1 alpha:0.45];
	stick_knob.userInteractionEnabled = NO;
	stick_knob.hidden = YES;
	[self addSubview:stick_base];
	[self addSubview:stick_knob];
	start_button = [self smallButton:@"Start" button:SDL_GAMEPAD_BUTTON_START];
	back_button = [self smallButton:@"Back" button:SDL_GAMEPAD_BUTTON_BACK];
	[self makeSensitivityControls];
	[self makeOverlayToggle];
	link = [CADisplayLink displayLinkWithTarget:self selector:@selector(tick)];
	[link addToRunLoop:NSRunLoop.mainRunLoop forMode:NSRunLoopCommonModes];
	[NSNotificationCenter.defaultCenter addObserver:self selector:@selector(applicationWillResignActive:)
		name:UIApplicationWillResignActiveNotification object:nil];
	[NSNotificationCenter.defaultCenter addObserver:self selector:@selector(applicationDidBecomeActive:)
		name:UIApplicationDidBecomeActiveNotification object:nil];
	return self;
}

- (void)applicationWillResignActive:(NSNotification *)notification
{
	inactive = YES;
	[self cancelInput];
	if (getenv("XG_TOUCH_TRACE")) NSLog(@"HaloPad Xbox: touch lifecycle inactive, input cleared");
}

- (void)applicationDidBecomeActive:(NSNotification *)notification
{
	[self cancelInput];
	inactive = NO;
	if (getenv("XG_TOUCH_TRACE")) NSLog(@"HaloPad Xbox: touch lifecycle active, fresh input required");
}

- (UIButton *)smallButton:(NSString *)title button:(int)button
{
	UIButton *view = [UIButton buttonWithType:UIButtonTypeSystem];
	[view setTitle:title forState:UIControlStateNormal];
	[view setTitleColor:[UIColor colorWithWhite:1 alpha:0.85] forState:UIControlStateNormal];
	view.titleLabel.font = [UIFont systemFontOfSize:14 weight:UIFontWeightSemibold];
	view.backgroundColor = [UIColor colorWithWhite:0 alpha:0.28];
	view.layer.cornerRadius = 14;
	view.tag = button;
	[view addTarget:self action:@selector(smallDown:) forControlEvents:UIControlEventTouchDown];
	[view addTarget:self action:@selector(smallUp:) forControlEvents:UIControlEventTouchUpInside | UIControlEventTouchUpOutside];
	[view addTarget:self action:@selector(smallCancel:) forControlEvents:UIControlEventTouchCancel];
	[self addSubview:view];
	return view;
}

/* look sensitivity: a "Sens" button opens a slider, kept in the user defaults */
- (void)makeSensitivityControls
{
	NSNumber *saved = [NSUserDefaults.standardUserDefaults objectForKey:LOOK_SENSITIVITY_KEY];
	look_sensitivity = saved ? fmaxf(LOOK_SENSITIVITY_MIN, fminf(LOOK_SENSITIVITY_MAX, saved.floatValue)) : 1.0f;
	sensitivity_button = [UIButton buttonWithType:UIButtonTypeSystem];
	[sensitivity_button setTitle:@"Sens" forState:UIControlStateNormal];
	[sensitivity_button setTitleColor:[UIColor colorWithWhite:1 alpha:0.85] forState:UIControlStateNormal];
	sensitivity_button.titleLabel.font = [UIFont systemFontOfSize:14 weight:UIFontWeightSemibold];
	sensitivity_button.backgroundColor = [UIColor colorWithWhite:0 alpha:0.28];
	sensitivity_button.layer.cornerRadius = 14;
	[sensitivity_button addTarget:self action:@selector(toggleSensitivity) forControlEvents:UIControlEventTouchUpInside];
	[self addSubview:sensitivity_button];
	sensitivity_panel = [UIView new];
	sensitivity_panel.backgroundColor = [UIColor colorWithWhite:0 alpha:0.6];
	sensitivity_panel.layer.cornerRadius = 14;
	sensitivity_panel.hidden = YES;
	sensitivity_label = [UILabel new];
	sensitivity_label.textColor = [UIColor colorWithWhite:1 alpha:0.9];
	sensitivity_label.font = [UIFont monospacedDigitSystemFontOfSize:14 weight:UIFontWeightSemibold];
	sensitivity_slider = [UISlider new];
	sensitivity_slider.minimumValue = LOOK_SENSITIVITY_MIN;
	sensitivity_slider.maximumValue = LOOK_SENSITIVITY_MAX;
	sensitivity_slider.value = look_sensitivity;
	[sensitivity_slider addTarget:self action:@selector(sensitivityChanged) forControlEvents:UIControlEventValueChanged];
	[sensitivity_panel addSubview:sensitivity_label];
	[sensitivity_panel addSubview:sensitivity_slider];
	[self addSubview:sensitivity_panel];
	[self updateSensitivityLabel];
}

/* the overlay can be turned off (a controller, a keyboard, or just watching): everything
hides and stops taking touches except a faint "Controls" button that turns it on again */
- (UIButton *)topButton:(NSString *)title action:(SEL)action
{
	UIButton *view = [UIButton buttonWithType:UIButtonTypeSystem];
	[view setTitle:title forState:UIControlStateNormal];
	[view setTitleColor:[UIColor colorWithWhite:1 alpha:0.85] forState:UIControlStateNormal];
	view.titleLabel.font = [UIFont systemFontOfSize:14 weight:UIFontWeightSemibold];
	view.backgroundColor = [UIColor colorWithWhite:0 alpha:0.28];
	view.layer.cornerRadius = 14;
	[view addTarget:self action:action forControlEvents:UIControlEventTouchUpInside];
	[self addSubview:view];
	return view;
}

- (void)makeOverlayToggle
{
	hide_button = [self topButton:@"Hide" action:@selector(hideOverlay)];
	show_button = [self topButton:@"Controls" action:@selector(showOverlay)];
	show_button.alpha = 0.18;
	[self setOverlayOff:[NSUserDefaults.standardUserDefaults boolForKey:OVERLAY_OFF_KEY]];
}

- (void)setOverlayOff:(BOOL)off
{
	if (off) [self cancelInput];
	overlay_off = off;
	for (UIView *view in self.subviews)
		view.hidden = off ? view != show_button : (view == show_button || view == stick_base || view == stick_knob || view == sensitivity_panel);
	[NSUserDefaults.standardUserDefaults setBool:off forKey:OVERLAY_OFF_KEY];
}

- (void)hideOverlay { [self setOverlayOff:YES]; }
- (void)showOverlay { [self setOverlayOff:NO]; }

- (void)updateSensitivityLabel
{
	sensitivity_label.text = [NSString stringWithFormat:@"Look sensitivity  %.2fx", look_sensitivity];
}

- (void)toggleSensitivity
{
	sensitivity_panel.hidden = !sensitivity_panel.hidden;
}

- (void)sensitivityChanged
{
	look_sensitivity = roundf(sensitivity_slider.value * 20) / 20; /* steps of 0.05 */
	[NSUserDefaults.standardUserDefaults setFloat:look_sensitivity forKey:LOOK_SENSITIVITY_KEY];
	[self updateSensitivityLabel];
}

- (void)smallDown:(UIButton *)sender
{
	if (inactive || self.hidden) return;
	state.buttons |= 1u << sender.tag; [self publish];
}
- (void)smallUp:(UIButton *)sender { state.buttons &= ~(1u << sender.tag); [self publish]; }
- (void)smallCancel:(UIButton *)sender { [self cancelInput]; }

- (CGRect)frameOfButton:(size_t)index
{
	CGSize size = self.bounds.size;
	UIEdgeInsets safe = self.safeAreaInsets;
	CGFloat s = buttons[index].size;
	return CGRectMake(size.width - safe.right - buttons[index].x - s / 2, size.height - safe.bottom - buttons[index].y - s / 2, s, s);
}

- (void)layoutSubviews
{
	UIEdgeInsets safe = self.safeAreaInsets;
	[super layoutSubviews];
	for (size_t index = 0; index < button_views.count; index++)
		button_views[index].frame = [self frameOfButton:index];
	start_button.frame = CGRectMake(self.bounds.size.width / 2 + 10, safe.top + 12, 70, 32);
	back_button.frame = CGRectMake(self.bounds.size.width / 2 - 80, safe.top + 12, 70, 32);
	sensitivity_button.frame = CGRectMake(self.bounds.size.width / 2 + 90, safe.top + 12, 60, 32);
	hide_button.frame = CGRectMake(self.bounds.size.width / 2 + 160, safe.top + 12, 60, 32);
	show_button.frame = CGRectMake(MAX(safe.left, 12) + 8, safe.top + 12, 84, 32);
	sensitivity_panel.frame = CGRectMake(self.bounds.size.width / 2 - 160, safe.top + 52, 320, 76);
	sensitivity_label.frame = CGRectMake(16, 8, 288, 24);
	sensitivity_slider.frame = CGRectMake(16, 36, 288, 32);
}

- (UIView *)hitTest:(CGPoint)point withEvent:(UIEvent *)event
{
	UIView *hit = [super hitTest:point withEvent:event];
	if (overlay_off) return hit == show_button ? hit : nil;   /* off: only the faint button takes touches */
	return self.hidden ? nil : hit;
}

- (int)buttonAt:(CGPoint)point
{
	for (size_t index = 0; index < sizeof(buttons) / sizeof(buttons[0]); index++)
		if (CGRectContainsPoint(CGRectInset([self frameOfButton:index], -8, -8), point))
			return (int)index;
	return -1;
}

- (void)setButton:(int)index down:(BOOL)down
{
	int button = buttons[index].button;
	if (button == -1) state.axes[4] = down ? 1 : 0;
	else if (button == -2) state.axes[5] = down ? 1 : 0;
	else if (down) state.buttons |= 1u << button;
	else state.buttons &= ~(1u << button);
	button_views[index].backgroundColor = [UIColor colorWithWhite:down ? 1 : 0 alpha:down ? 0.35 : 0.28];
}

- (void)touchesBegan:(NSSet<UITouch *> *)touches withEvent:(UIEvent *)event
{
	if (inactive || self.hidden || overlay_off) return;
	for (UITouch *touch in touches)
	{
		CGPoint point = [touch locationInView:self];
		int button = [self buttonAt:point];
		if (button >= 0)
		{
			[button_touches setObject:@(button) forKey:touch];
			[self setButton:button down:YES];
		}
		else if (point.x < self.bounds.size.width * 0.4 && !stick_touch)
		{
			stick_touch = touch;
			stick_center = stick_point = point;
			stick_base.center = stick_knob.center = point;
			stick_base.hidden = stick_knob.hidden = NO;
		}
		else if (!look_touch)
		{
			look_touch = touch;
			look_last = point;
			look_time = touch.timestamp;
			look_velocity = CGPointZero;
		}
	}
	[self publish];
}

- (void)touchesMoved:(NSSet<UITouch *> *)touches withEvent:(UIEvent *)event
{
	for (UITouch *touch in touches)
	{
		CGPoint point = [touch locationInView:self];
		if (touch == stick_touch)
		{
			CGFloat dx = point.x - stick_center.x, dy = point.y - stick_center.y, length = hypot(dx, dy);
			if (length > STICK_RADIUS)
			{
				dx *= STICK_RADIUS / length;
				dy *= STICK_RADIUS / length;
			}
			stick_knob.center = CGPointMake(stick_center.x + dx, stick_center.y + dy);
			state.axes[0] = (float)(dx / STICK_RADIUS);
			state.axes[1] = (float)(dy / STICK_RADIUS);
		}
		else if (touch == look_touch)
		{
			CFTimeInterval dt = MAX(touch.timestamp - look_time, 1.0 / 240.0);
			look_velocity = CGPointMake((point.x - look_last.x) / dt, (point.y - look_last.y) / dt);
			look_last = point;
			look_time = touch.timestamp;
		}
	}
	[self publish];
}

- (void)touchesEnded:(NSSet<UITouch *> *)touches withEvent:(UIEvent *)event
{
	for (UITouch *touch in touches)
	{
		NSNumber *button = [button_touches objectForKey:touch];
		if (button)
		{
			[button_touches removeObjectForKey:touch];
			/* Multiple fingers may own the same button. Only its last release
			 * clears the held action and its highlight. */
			if (![[[button_touches objectEnumerator] allObjects] containsObject:button])
				[self setButton:button.intValue down:NO];
		}
		if (touch == stick_touch)
		{
			stick_touch = nil;
			state.axes[0] = state.axes[1] = 0;
			stick_base.hidden = stick_knob.hidden = YES;
		}
		if (touch == look_touch)
		{
			look_touch = nil;
			look_velocity = CGPointZero;
		}
	}
	[self publish];
}

- (void)cancelInput
{
	stick_touch = look_touch = nil;
	look_velocity = CGPointZero;
	stick_base.hidden = stick_knob.hidden = YES;
	[button_touches removeAllObjects];
	memset(&state, 0, sizeof(state));
	for (UIView *view in button_views)
		view.backgroundColor = [UIColor colorWithWhite:0 alpha:0.28];
	xg_ios_clear_touch_pad();
}

- (void)touchesCancelled:(NSSet<UITouch *> *)touches withEvent:(UIEvent *)event { [self cancelInput]; }

/* the right stick follows the look finger's speed, and eases off when it stops */
- (void)tick
{
	/* XG_TOUCH_SHOW: keep it on screen with a controller connected (development) */
	BOOL controller = xg_ios_controller_connected() && !getenv("XG_TOUCH_SHOW");
	if (self.hidden != controller)
	{
		if (controller) [self cancelInput];
		NSLog(@"HaloPad Xbox: touch gamepad %@ (controller %d), frame %@", controller ? @"hidden" : @"shown", controller,
			NSStringFromCGRect(self.frame));
		self.hidden = controller;
	}
	if (look_touch && CACurrentMediaTime() - look_time > 0.05)
		look_velocity = CGPointMake(look_velocity.x * 0.5, look_velocity.y * 0.5);
	[self publish];
}

- (void)publish
{
	if (inactive || self.hidden) return;
	/* Publish look on the touch event, not only on the next display tick. */
	state.axes[2] = (float)fmax(-1.0, fmin(1.0, look_velocity.x * look_sensitivity / LOOK_SPEED));
	state.axes[3] = (float)fmax(-1.0, fmin(1.0, look_velocity.y * look_sensitivity / LOOK_SPEED));
	xg_ios_set_touch_pad(&state);
}

@end
