"""Dependency-free Ed25519 (RFC 8032), with strict encodings/subgroup checks.

Verification handles public inputs. Signing is an offline release utility, not
constant-time: keep signing seeds on a trusted release machine.
"""
import hashlib

P = 2**255 - 19
L = 2**252 + 27742317777372353535851937790883648493
D = -121665 * pow(121666, P-2, P) % P
I = pow(2, (P-1)//4, P)
IDENTITY = (0, 1, 1, 0)


def _decode(data):
    if len(data) != 32:
        raise ValueError('Invalid point length')
    encoded = int.from_bytes(data, 'little')
    y, sign = encoded & (2**255-1), encoded >> 255
    if y >= P:
        raise ValueError('Noncanonical point')
    x2 = (y*y-1) * pow(D*y*y+1, P-2, P) % P
    x = pow(x2, (P+3)//8, P)
    if x*x % P != x2:
        x = x*I % P
    if x*x % P != x2 or (x == 0 and sign):
        raise ValueError('Invalid point')
    if x & 1 != sign:
        x = P-x
    return x, y, 1, x*y % P


def _add(a, b):
    x, y, z, t = a
    u, v, w, s = b
    aa, bb = (y-x)*(v-u) % P, (y+x)*(v+u) % P
    cc, dd = 2*D*t*s % P, 2*z*w % P
    e, f, g, h = bb-aa, dd-cc, dd+cc, bb+aa
    return e*f % P, g*h % P, f*g % P, e*h % P


def _mul(point, scalar):
    result = IDENTITY
    while scalar:
        if scalar & 1:
            result = _add(result, point)
        point = _add(point, point)
        scalar >>= 1
    return result


def _encode(point):
    x, y, z, _ = point
    inverse = pow(z, P-2, P)
    return ((y*inverse % P) | ((x*inverse % P & 1) << 255)).to_bytes(32, 'little')


B = _decode(bytes.fromhex('58' + '66'*31))


def verify(public_key, message, signature):
    try:
        if len(public_key) != 32 or len(signature) != 64:
            return False
        a, r = _decode(public_key), _decode(signature[:32])
        scalar = int.from_bytes(signature[32:], 'little')
        neutral = _encode(IDENTITY)
        if scalar >= L or any(_encode(q) == neutral or _encode(_mul(q, L)) != neutral for q in (a, r)):
            return False
        challenge = int.from_bytes(hashlib.sha512(signature[:32]+public_key+message).digest(), 'little') % L
        return _encode(_mul(B, scalar)) == _encode(_add(r, _mul(a, challenge)))
    except (ValueError, TypeError, OverflowError):
        return False


def _secret(seed):
    if len(seed) != 32:
        raise ValueError('Ed25519 seed must contain 32 bytes')
    hashed = hashlib.sha512(seed).digest()
    scalar = (int.from_bytes(hashed[:32], 'little') & (2**254-8)) | 2**254
    return scalar, hashed[32:]


def public_key(seed):
    scalar, _ = _secret(seed)
    return _encode(_mul(B, scalar))


def sign(seed, message):
    scalar, prefix = _secret(seed)
    public = _encode(_mul(B, scalar))
    nonce = int.from_bytes(hashlib.sha512(prefix+message).digest(), 'little') % L
    r = _encode(_mul(B, nonce))
    challenge = int.from_bytes(hashlib.sha512(r+public+message).digest(), 'little') % L
    return r + ((nonce+challenge*scalar) % L).to_bytes(32, 'little')
