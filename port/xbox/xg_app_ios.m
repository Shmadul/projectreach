/*
 * xg_app_ios.m: a stand-alone iOS test app for the Xbox engine (development
 * only; HaloPad's own app will offer the engine from its launch picker).
 * Data: XG_DATA and XG_SAVE from the environment (simctl), else Documents.
 */
#import <UIKit/UIKit.h>
#include "xg_ios.h"
#include <stdio.h>
#include <unistd.h>

@interface XGViewController : UIViewController
@end

@implementation XGViewController
- (void)loadView
{
	UIView *root = [[UIView alloc] initWithFrame:UIScreen.mainScreen.bounds];
	UIView *game = xg_ios_make_view(root.bounds);
	XGTouchPad *pad = [[XGTouchPad alloc] initWithFrame:root.bounds];
	root.backgroundColor = UIColor.blackColor;
	game.autoresizingMask = pad.autoresizingMask = UIViewAutoresizingFlexibleWidth | UIViewAutoresizingFlexibleHeight;
	[root addSubview:game];
	[root addSubview:pad];         /* the touch gamepad above the game; it hides while a controller is connected */
	self.view = root;
}
- (void)viewDidLayoutSubviews { [super viewDidLayoutSubviews]; xg_ios_view_resized(); }
- (BOOL)prefersStatusBarHidden { return YES; }
- (BOOL)prefersHomeIndicatorAutoHidden { return YES; }
- (UIInterfaceOrientationMask)supportedInterfaceOrientations { return UIInterfaceOrientationMaskLandscape; }
- (void)viewDidAppear:(BOOL)animated
{
	static int started;
	NSString *documents = NSSearchPathForDirectoriesInDomains(NSDocumentDirectory, NSUserDomainMask, YES).firstObject;
	NSDictionary *environment = NSProcessInfo.processInfo.environment;
	NSString *data = environment[@"XG_DATA"] ?: documents;
	NSString *save = environment[@"XG_SAVE"] ?: [documents stringByAppendingPathComponent:@"save"];
	NSString *image = [NSBundle.mainBundle pathForResource:@"halo_guest" ofType:@"elf"];
	[super viewDidAppear:animated];
	if (started++)
		return;
	/* the console (internet play's joins, NAT and connection results) also goes to
	Documents/console.txt, so a session can be read afterwards; the previous one is kept
	as console-previous.txt. Not when stderr is a terminal; XG_NO_CONSOLE_FILE turns it off. */
	if (!isatty(STDERR_FILENO) && !getenv("XG_NO_CONSOLE_FILE"))
	{
		NSString *log = [data stringByAppendingPathComponent:@"console.txt"];
		NSString *previous = [data stringByAppendingPathComponent:@"console-previous.txt"];
		[NSFileManager.defaultManager removeItemAtPath:previous error:nil];
		[NSFileManager.defaultManager moveItemAtPath:log toPath:previous error:nil];
		if (freopen(log.fileSystemRepresentation, "a", stderr))
			setvbuf(stderr, NULL, _IOLBF, 0);
	}
	/* online play's MQTT brokers (network.brokers_file, beside config.toml), written at each
	start as on Android: without them the server browser finds no games */
	NSData *brokers = [NSData dataWithContentsOfFile:[NSBundle.mainBundle pathForResource:@"brokers" ofType:@"txt"]];
	if (!brokers || ![brokers writeToFile:[data stringByAppendingPathComponent:@"brokers.txt"] atomically:YES])
		NSLog(@"HaloPad Xbox: could not write brokers.txt; the server browser will find no games");
	if (!image || xg_ios_start(image.fileSystemRepresentation, data.fileSystemRepresentation, save.fileSystemRepresentation))
		NSLog(@"HaloPad Xbox engine did not start");
}
@end

@interface XGAppDelegate : UIResponder <UIApplicationDelegate>
@property(nonatomic, strong) UIWindow *window;
@end

@implementation XGAppDelegate
- (BOOL)application:(UIApplication *)application didFinishLaunchingWithOptions:(NSDictionary *)options
{
	self.window = [[UIWindow alloc] initWithFrame:UIScreen.mainScreen.bounds];
	self.window.rootViewController = [XGViewController new];
	[self.window makeKeyAndVisible];
	return YES;
}
@end

int main(int argc, char *argv[])
{
	@autoreleasepool { return UIApplicationMain(argc, argv, nil, NSStringFromClass([XGAppDelegate class])); }
}
