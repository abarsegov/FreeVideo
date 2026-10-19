"""Sign a source manifest using a private 32-byte Ed25519 seed file (raw or hex).

The private file stays outside the repository. Output replaces only the public
manifest and its SHA-256 sidecar. No secret or private pathname is printed.
"""
import argparse
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import freevideo_ed25519 as ed25519
import freevideo_source_bootstrap as source

DOMAIN = b'FreeVideo source manifest v1\x00'


def signing_bytes(value):
    return DOMAIN + source.canonical({k: v for k, v in value.items() if k != 'signature'})


def sign_manifest(path, private_key):
    value = source.read_manifest(path)
    raw = private_key.read_bytes()
    seed = raw if len(raw) == 32 else bytes.fromhex(raw.decode('ascii').strip())
    value['signature'] = ed25519.sign(seed, signing_bytes(value)).hex()
    data = source.canonical(value)
    path.write_bytes(data)
    path.with_suffix('.json.sha256').write_text(source.sha(data) + '\n', encoding='ascii')


UPDATE_DOMAIN = b'FreeVideo update manifest v1\x00'


def sign_update_manifest(path, private_key):
    """Write <name>.sig (hex) beside an update-*.json; the manifest stays byte-identical."""
    raw = private_key.read_bytes()
    seed = raw if len(raw) == 32 else bytes.fromhex(raw.decode('ascii').strip())
    signature = ed25519.sign(seed, UPDATE_DOMAIN + path.read_bytes()).hex()
    path.with_name(path.name + '.sig').write_text(signature + '\n', encoding='ascii')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', type=Path, required=True)
    parser.add_argument('--private-key', type=Path, required=True)
    parser.add_argument('--update-manifest', action='store_true',
                        help='the manifest is an update-<platform>.json: write a detached <name>.sig')
    args = parser.parse_args()
    if args.update_manifest:
        sign_update_manifest(args.manifest, args.private_key)
        print('Wrote the detached signature for the update manifest.')
        return
    sign_manifest(args.manifest, args.private_key)
    print('Signed source manifest and refreshed SHA-256 sidecar.')


if __name__ == '__main__':
    main()
