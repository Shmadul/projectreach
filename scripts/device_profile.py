#!/usr/bin/env python3
"""Preflight an iOS development profile before signing or installing HaloPad."""
import argparse
import datetime
import hashlib
import pathlib
import plistlib
import re
import subprocess

REQUIRED = (
    'com.apple.developer.kernel.extended-virtual-addressing',
    'com.apple.developer.kernel.increased-memory-limit',
)


def decode(path):
    result = subprocess.run(['security', 'cms', '-D', '-i', str(path)], check=True, capture_output=True)
    return plistlib.loads(result.stdout)


def signing_identity(selector):
    result = subprocess.run(['security', 'find-identity', '-v', '-p', 'codesigning'], check=True,
                            capture_output=True, text=True)
    found = re.findall(r'^\s*\d+\) ([0-9A-F]{40}) "([^"]+)"', result.stdout, re.M)
    matches = [sha for sha, name in found if selector.upper() == sha or selector == name]
    if len(matches) != 1:
        raise ValueError(f'signing identity {selector!r} matched {len(matches)} certificates; use its unique SHA-1 from security find-identity')
    return matches[0]


def validate(profile, bundle, identity_sha, device=None, now=None):
    errors = []
    ent = profile.get('Entitlements', {})
    teams = profile.get('TeamIdentifier', [])
    team = teams[0] if len(teams) == 1 else None
    if not team or ent.get('com.apple.developer.team-identifier') != team:
        errors.append('profile team is missing or inconsistent')
    if not team or ent.get('application-identifier') != f'{team}.{bundle}':
        errors.append(f'profile App ID must be exactly {bundle} (no wildcard)')
    # a development profile, or an ad hoc one: both install on the devices they list
    if ent.get('get-task-allow') is not True and not profile.get('ProvisionedDevices'):
        errors.append('profile is neither a development nor an ad hoc profile')
    for key in REQUIRED:
        if ent.get(key) is not True:
            errors.append(f'profile does not grant {key}')
    expiry = profile.get('ExpirationDate')
    if not isinstance(expiry, datetime.datetime) or expiry <= (now or datetime.datetime.now(datetime.timezone.utc)).replace(tzinfo=None):
        errors.append('profile is expired or has no expiration date')
    certs = profile.get('DeveloperCertificates', [])
    fingerprints = {hashlib.sha1(cert).hexdigest().upper() for cert in certs if isinstance(cert, bytes)}
    if identity_sha.upper() not in fingerprints:
        errors.append('signing certificate is not included in the profile')
    if device and device not in profile.get('ProvisionedDevices', []):
        errors.append('device UDID is not included in the profile')
    if errors:
        raise ValueError('; '.join(errors))
    return ent


def check(path, bundle, identity, device=None):
    return validate(decode(path), bundle, signing_identity(identity), device)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--profile', required=True, type=pathlib.Path)
    ap.add_argument('--bundle', default='dev.halopad.HaloPad')
    ap.add_argument('--identity', required=True)
    ap.add_argument('--device', help='physical device UDID')
    a = ap.parse_args()
    try:
        check(a.profile, a.bundle, a.identity, a.device)
    except (ValueError, subprocess.CalledProcessError, plistlib.InvalidFileException) as exc:
        ap.exit(1, f'profile preflight failed: {exc}\n')
    print('PASS: profile App ID, certificate, expiry, development and memory entitlements'
          + (', device' if a.device else ''))


if __name__ == '__main__':
    main()
