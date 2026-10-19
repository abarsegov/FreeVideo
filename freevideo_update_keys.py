"""Release trust anchor: the public half of the update-signing key. Never ship a private key."""

# RFC 8032 test key; it never authorizes anything and only marks an unreplaced key.
PLACEHOLDER_PUBLIC_KEY = bytes.fromhex('d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a')
# Public key; SHA-256 7d3681f25aec9e54f249d0e877abdabb50de82af8ff0348d5c80fa124a748912.
ED25519_PUBLIC_KEY = bytes.fromhex('4db79633766fe1fbe01b53a0700021a001051466f03a4de86d2719dd7d44424c')
ED25519_RELEASE_KEY_ENABLED = True
