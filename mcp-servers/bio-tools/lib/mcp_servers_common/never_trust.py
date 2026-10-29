"""Certificates the bundled MCP servers never trust, whatever roots a
connection was checked against. A handshake that happens inside a connection's
first read or write, rather than in a do_handshake call, is not checked.
"""

from __future__ import annotations

import _ssl
import contextlib
import functools
import hashlib
import ssl





NEVER_TRUST = frozenset({
    "86a1ecba089c4a8d3bbe2734c612ba341d813e043cf9e8a862cd5c57a36bbe6b",
    "8d417db2dd8bf5e3084d1e3f196d583849d81bdd4c00c70b9d39369e96b8c782",
    "bc4d809b15189d78db3e1d8cf4f9726a795da1643ca5f1358e1ddb0edc0d7eb3",
    "eabc185c4e82d942b1a5978ba3c0181487d6b3b9974e5c49f72f6d0bd9637150",
    "40f6af0346a99aa1cd1d555a4e9cce62c7f9634603ee406615833dc8c8d00367",
    "376a1a7082a593dccc20d561d119e9ab8d30f11cc321d0a37fa41f0df284e01c",
    "125609aa301da0a249b97a8239cb6a34216f44dcac9f3954b14292f2e8c8608f",
    "b7408b4d2be0238ba37004dd34e276c6019bd2f24c9db7d4980f5f6c359a4bcc",
    "f2edaaff3d9b701fb4d14c9f285f8c0bad90fc3bcc1fa07d47f4718edaba34a5",
    "0a6c2b2ca5b364207954171987ac9d2178561569d8205457e77ca90c32ede282",
    "7dd78d5f4f13459a83dff9abbba62edbaf6f2d102bf257fd712f4d9f2746ed8d",
    "c619e4780ed8e0abeda24ad732567de4383a7ea941338f6d1a7d1b49ac792fe9",
    "46cd083b47e80402028df493960ea19c85fe851950d5165f1c7da4faa951e2f8",
    "3f83a68c4e80fc3f4f487e3105ad146498e56a09a3ea223b2a2982943f5995d7",
    "1aa0c2709e831bd6e3b5129a00ba41f7eeef020872f1e6504bf0f6c3f24f3af3",
    "71f8adeec2b58adbdf6205c98dde928a4254b03695193d7f9a63a9a2588095d0",
    "0d74ef67c88de3560365e225aef976f915e901347b73352d81ab5b4132bb2150",
    "941ea0cbce0926d9f34ec667cee669b61fd39f4338fa0b91048d5749d8880d25",
    "8abfc40faeaf13cbc8bb67b61c8d097a321cd2fee047c54256927e0f8bfeadfd",
    "b5ee1c904ac2aae39c92601ab466269c0c1cd0b96badbd251b0ed88e6c4ded68",
    "bbd8b3d745efb79198d93b9c482b9d934537325729ea2f02c129261f91be2e3d",
    "8dd3d055a7f47ffbca287285dcfd201dde92823ccc484d30f11cc40f079b3a3e",
    "47911bc8f32a11b029e0bb10a348b459404b87731b870a2f3fa97b4705dc4c25",
    "33836758bd871b8d464bd669b0b81832880b81e363fb8cbb21ba8e843a138858",
    "f76a0d440c418dc5b362be8e11e0a5d06023f4396093e4f455827bf55153d8a4",
    "5da77dc800f4c6fa93c6015aa6bd898fdb7c28d869e764571b25c00375a77755",
    "cef71e70b7c29addf6c30cd19e614b38fd5f02a435a0eeddd0087e183d101a51",
    "ece28e35289ce8621c72407949e5050052ad4607cc332eaf9b546029aacbfb7a",
    "1997770a5633fb6d0440abf7d7e87e15d795f094f8c2959dadb00e785866bf48",
    "1cbbd4cb5eec6a287e34636c07c78378fa3eb3c6d3fb0563286870f283d87034",
    "75d2ce75979cf2a43b172c9f89f733dda2136031db4bbf3698b053714c077155",
    "11567f2aaf4325c4044880c6ed2b37f1ca2586d87059cf254934d97c4fee89a5",
    "c626090fd48ddbf2eee327e6a3d2ca78bd350c1faf0ceff015d834c3bd176ab0",
    "cf36ee3b065cb7405569f6c189b54c2f3d43042a995e3f5fcfccefe09ad73177",
    "be0c7a1e9ed59990c3060095c2d6a9fd55a6e9448728f3235c7c3c0108fa2f43",
    "e3212ac258d6d86f464c1150866a298c04a42783c1d17d7baa6a24ac0323f139",
    "d7a7a0fb5d7e2731d771e9484ebcdef71d5f0c3e0a2948782bc83ee0ea699ef4",
    "bd153ed7b0434f6886b17bce8bbe84ed340c7132d702a8f4fa318f756ecbd6f3",
    "ebed6475c2c2d80fcb199f3fe8dd6e6b3ce60b77eb15d4e42c3dec0c6f5b5105",
    "49dd353dc2b919a3770645117d0e60c16115e7d1bc259eefe734b00ea434fe5f",
    "e0254c7fcdc265e2f0f8a428dabb75ab4ae039b4872d62f5ee57f0877d5a8a41",
    "5c0660a2b164910d8ff589c18e14552e9e0ec237dcefe2cb736d41a4811f81a0",
    "ec4ffa1ad0ac88081314b7aaffef6a91f97cd57291bb5ba686c5415f5eb4f6b6",
    "92c063d67e8f14db71d288e11266ffdaf69cbf115f7bab14f3bc2f68d25e0e30",
    "43bffa19d6ef651451f6841e019931e96f6f2eaeff28ab4b6f1ce4576de2717b",
    "63ecefee431d49523bc1136c4e72d0206a477086caf606cd5cf025686c109cf8",
    "0169613548123dab14afffcb5a3f0e804c70ac903f29ecad55ed43dc75f13881",
    "04db3b7ddf683b1d4b5b64fe743a3a8d8c3d71f0c339d7dc25b343e665a9eb5b",
    "12dd68cd491523b8e627ab6f192d32d6cbb3655275c17afa61bc511e7ca34d4c",
    "1a942e3925b809896b4b291a8cd2e7bc5f1d533aa523b8353b2df377b0f0adbe",
    "27616f251b91b3df6a241b45854a5db0a7b1bedd13a296f5cacb670aeac460e5",
    "2ff90b0e2bd10089c13500a8cb86bee9027f7a71f12a1c074aa07bb8a08e2724",
    "32159657965e44ec9b1b7f0933475009e50479a0fae1dfbb236a8dad4ff7a6a6",
    "34556838281688d1c967d43d1372a1bbaf389277222f03e1017b18b348a1f00e",
    "4aa7e0e79901d2709e94750e98a013e64dcbdc219c23bdea36b0afaaec4f12d9",
    "4e8b3000146768d220e3b7fbc2bd2f46bfb3f76c563f82f0ab8a9b2563eeef00",
    "6ca6b0e819309539b9627e00aa70d74a0919800f8f217e6197fb6946989ed264",
    "8429351035ba90ba14ef3f7ae38ca35480605a0296aa6c263c6a9b26bb9a48e1",
    "8c7870d7e1c55d0f06d77db49843efaf017371b7e28f4a71241ff1746f04a5fc",
    "aa4216dc80a7299ae6de08444bea2c588dc873631ebc563c0776d83a1b61e3ce",
    "c073efb31422ff4db99dc24d7442438e942c73cd3c61b529a8ba32542e946b86",
    "c2690b6bf35a9364e697735ec630bfd9d2cd59040e3908a66765168e3282b04e",
    "c6fd1e9191185d5e2a09fc5d35674e0292b54ed426a98e3dc91b98443b186f0a",
    "ceb5298daeb8f59279480b46002c89f7755cc3a169514b11b810befc195a00d8",
    "d16f35a2fc245c9aea23050fb48f69240aeb2892466cf998c518139fbd0fec0e",
    "d45ce41f3b285436b8fd3e2b68e500f2835dd737035f2bcc489e9e48cf0cccd8",
    "d8aaa5319de5242ec1734b988bdf330fec7dbd38206caf76656ee4836a216876",
    "daa9724da8e1fe403aba0b412467db4e4b376c631eea8dcea4960929f4aeb9b8",
    "f8db20c2d076249967beea91ec27c954a5d405863e0ed1231c3b8c7f1fbfc7ff",
    "fff112fc55c9d5b2616a45455c326d290a17d38f06ec21116d624425da822728",
})

LISTED = ("a certificate in the site's chain is one this app never trusts, "
          "and the app's never-trust list cannot be switched off; whoever "
          "runs the site (or a proxy that inspects TLS on the way) can fix "
          "this")
UNREAD = ("this app refused a certificate chain that it could not check "
          "against its never-trust list; please report this to Anthropic")
RESUMED = ("this app refused a resumed TLS session, which brings no verified "
           "certificate chain to check against its never-trust list; please "
           "report this to Anthropic")


_refusals: list[str] = []
_INSTALLED = "_never_trust_installed"
_SHUT_RDWR = 2


def _element(der: bytes, at: int) -> tuple[int, int, int]:
    """The DER element at `at`: its tag, where its content starts, and where
    the next element starts."""
    first, content = der[at + 1], at + 2
    if der[at] & 0x1F == 0x1F:
        raise ValueError("not DER as expected: a tag in the long form")
    if first < 0x80:
        length = first
    else:
        content += first & 0x7F
        length = int.from_bytes(der[at + 2:content], "big")
    if first == 0x80 or content + length > len(der):
        raise ValueError(
            "not DER: a length is indefinite or runs past the end of the data")
    return der[at], content, content + length


def public_key_info(der: bytes) -> bytes:
    """The SubjectPublicKeyInfo of a DER certificate, byte for byte as it sits
    there. Raises IndexError or ValueError when it cannot find that field, and
    when a length on the way to it is of the indefinite form, which OpenSSL
    3.0.13 and 3.6.4 accept inside tbsCertificate. A length written in more
    bytes than it needs is still read."""



    _, at, _ = _element(der, 0)
    _, at, _ = _element(der, at)
    tag, _, after = _element(der, at)
    if tag == 0xA0:
        at = after
    for _ in range(5):
        _, _, at = _element(der, at)
    tag, _, after = _element(der, at)
    if tag != 0x30:
        raise ValueError("no SEQUENCE where the key should be")
    return der[at:after]


def _inside(der: bytes, at: int, tag: int) -> tuple[int, int]:
    """Where the content of the element at `at` starts and where the next
    element starts. Raises ValueError unless its tag is `tag`."""
    found, content, after = _element(der, at)
    if found != tag:
        raise ValueError(f"not DER as expected: tag {found:#x}, not {tag:#x}")
    return content, after


def _der(tag: int, content: bytes) -> bytes:
    """One DER element, its length in the shortest form."""
    size = len(content)
    if size >= 0x80:
        octets = size.to_bytes((size.bit_length() + 7) // 8, "big")
        return bytes([tag, 0x80 | len(octets)]) + octets + content
    return bytes([tag, size]) + content


_OID_RSA = bytes.fromhex("2a864886f70d010101")

_RSA_OIDS = {
    _OID_RSA,
    bytes.fromhex("2a864886f70d01010a"),
    bytes.fromhex("55080101"),
}
_OID_EC = bytes.fromhex("2a8648ce3d0201")

_CURVES = {
    bytes.fromhex("2a8648ce3d030107"): (
        2**256 - 2**224 + 2**192 + 2**96 - 1, int(
            "5ac635d8aa3a93e7b3ebbd55769886bc651d06b0cc53b0f63bce3c3e27d2604b",
            16)),
    bytes.fromhex("2b81040022"): (
        2**384 - 2**128 - 2**96 + 2**32 - 1, int(
            "b3312fa7e23ee7e4988e056be3f82d19181d9c6efe8141120314088f5013875a"
            "c656398d8a2ed19d2a85c8edd3ec2aef", 16)),
    bytes.fromhex("2b81040023"): (
        2**521 - 1, int(
            "0051953eb9618e1c9a1f929a21a0b68540eea2da725b99b315f3b8b489918ef1"
            "09e156193951ec7e937b1652c0bd3bb1bf073573df883d2c34f1ef451fd46b50"
            "3f00", 16)),
}


def canonical_key(info: bytes, *, alone: bool = False) -> bytes:
    """A SubjectPublicKeyInfo in one standard form, so that the ways of writing
    one key that OpenSSL reads alike hash alike. An RSA key, under any of the
    three identifiers, becomes rsaEncryption with a NULL parameter (RFC 3279);
    an EC key that names P-256, P-384 or P-521 gets its point uncompressed
    (RFC 5480). The app's own key hashes, made under Bun, have that form for
    every such key Bun can read; Bun cannot read one under RSASSA-PSS. Another
    kind of key, or another named curve, comes back as it is. So does a curve
    spelt out when its certificate is `alone` in the verified chain, the only
    place where OpenSSL 3.0.13 and 3.6.4 took one. Raises IndexError or
    ValueError on a key it cannot read (a tag or length it does not expect,
    bits called unused, a point in no known form, a compressed point that is
    not on its curve) and on a curve spelt out that is not `alone`."""
    at, _ = _inside(info, 0, 0x30)
    at, bits_at = _inside(info, at, 0x30)
    start, parameters_at = _inside(info, at, 0x06)
    kind = info[start:parameters_at]
    if kind == _OID_EC:
        if info[parameters_at] == 0x30 and alone:
            return info
        start, after = _inside(info, parameters_at, 0x06)
        curve = info[start:after]
        if curve not in _CURVES:
            return info
    elif kind not in _RSA_OIDS:
        return info
    start, end = _inside(info, bits_at, 0x03)
    if info[start]:
        raise ValueError("not DER as expected: a key with unused bits")
    written = info[start + 1:end]
    if kind != _OID_EC:
        at, _ = _inside(written, 0, 0x30)
        numbers = b""
        for _ in range(2):
            start, at = _inside(written, at, 0x02)
            number = int.from_bytes(written[start:at], "big")
            numbers += _der(0x02, number.to_bytes(
                number.bit_length() // 8 + 1, "big"))
        algorithm = _der(0x06, _OID_RSA) + b"\x05\x00"
        key = _der(0x30, numbers)
    else:
        p, b = _CURVES[curve]
        size = (p.bit_length() + 7) // 8
        form, x = written[0], int.from_bytes(written[1:1 + size], "big")
        if form in (2, 3) and len(written) == 1 + size:
            square = (x * x * x - 3 * x + b) % p
            y = pow(square, (p + 1) // 4, p)
            if y * y % p != square:
                raise ValueError("an EC key that is not a point of its curve")
            if y & 1 != form & 1:
                y = p - y
            key = b"\x04" + written[1:] + y.to_bytes(size, "big")
        elif form in (4, 6, 7) and len(written) == 1 + 2 * size:


            key = b"\x04" + written[1:]
        else:
            raise ValueError("an EC key in no known form")
        algorithm = _der(0x06, _OID_EC) + _der(0x06, curve)
    return _der(0x30, _der(0x30, algorithm) + _der(0x03, b"\x00" + key))


def _verified_chain(tls: ssl.SSLSocket | ssl.SSLObject) -> list[bytes]:
    """DER certificates, the server's own first. Empty on a resumed session."""
    if hasattr(tls, "get_verified_chain"):
        return tls.get_verified_chain()

    return [cert.public_bytes(_ssl.ENCODING_DER)
            for cert in tls._sslobj.get_verified_chain() or ()]


def _hashed(chain: list[bytes]):
    """Of each certificate: itself, its key as written, its key in one form."""
    for der in chain:
        yield der
        yield (info := public_key_info(der))
        yield canonical_key(info, alone=len(chain) == 1)


def _objection(tls: ssl.SSLSocket | ssl.SSLObject) -> str | None:
    """Why the verified chain is refused, `LISTED`, `UNREAD` or `RESUMED`; None
    if it is accepted."""
    try:
        chain = _verified_chain(tls)
        listed = not NEVER_TRUST.isdisjoint(
            hashlib.sha256(part).hexdigest() for part in _hashed(chain))
    except Exception:
        return UNREAD
    return LISTED if listed else None if chain else RESUMED


def _refusal(why: str) -> ssl.SSLCertVerificationError:
    """An error shaped like the one _ssl raises when verification fails."""
    exc = ssl.SSLCertVerificationError(
        ssl.SSL_ERROR_SSL.value, "[SSL: CERTIFICATE_VERIFY_FAILED] "
        f"certificate verify failed: {why}")
    exc.library, exc.reason = "SSL", "CERTIFICATE_VERIFY_FAILED"

    exc.verify_code, exc.verify_message = 50, why
    return exc


def _checked(do_handshake):
    @functools.wraps(do_handshake)
    def wrapper(self, *args, **kwargs):
        do_handshake(self, *args, **kwargs)
        if (not self.server_side
                and self.context.verify_mode != ssl.CERT_NONE
                and (why := _objection(self))):
            if not _refusals:
                site = self.server_hostname
                _refusals.append(
                    f"{why} (refused site: {site})" if site else why)

            if isinstance(self, ssl.SSLSocket):
                with contextlib.suppress(OSError):
                    self.shutdown(_SHUT_RDWR)
                with contextlib.suppress(OSError):
                    self.close()
            else:
                with contextlib.suppress(ssl.SSLError):
                    self.unwrap()
            raise _refusal(why)

    setattr(wrapper, _INSTALLED, True)
    return wrapper


@contextlib.contextmanager
def surfaced():
    """Wrap one tool call. If the never-trust check refused a connection during
    the call, raise the first refusal when the call ends, even if the tool
    caught it. The notes are process-wide and cleared on entry, so enter only
    while holding the lock that keeps calls one at a time. A thread that a tool
    leaves running can still note a refusal into the next call."""
    _refusals.clear()
    try:
        yield
    except Exception as exc:
        if not _refusals:
            raise
        raise _refusal(_refusals[0]) from exc
    if _refusals:
        raise _refusal(_refusals[0])


def install() -> None:
    """From now on, a verifying TLS client handshake made by do_handshake on
    ssl.SSLSocket or ssl.SSLObject raises ssl.SSLCertVerificationError if the
    verified chain holds a certificate or key in `NEVER_TRUST`, is unreadable,
    or is empty, as on every resumed session. A second call changes nothing."""
    for cls in (ssl.SSLSocket, ssl.SSLObject):
        if not getattr(cls.do_handshake, _INSTALLED, False):
            cls.do_handshake = _checked(
                cls.do_handshake)
