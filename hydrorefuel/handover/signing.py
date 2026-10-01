"""交接签名与内容摘要。

签署不是"留个名字"：签名必须覆盖被冻内容，使冻结版和更正事后改不动。
这里用标准库 HMAC-SHA256 对规范化后的内容签名：

- :func:`canonical_json` 把字典按键名排序后序列化，保证同一内容永远得到
  同一串字节，不受插入顺序影响；
- :func:`digest_content` 给内容算 SHA-256 摘要，作为"这一版冻的是什么"
  的指纹，版本号、签名和审计事件都引用它；
- :func:`sign_content` / :func:`verify_signature` 用班次密钥对
  ``签名人|内容摘要`` 做 HMAC，校验签名人确实持有密钥且内容未被改。

这是"共享密钥 + 内容指纹"的完整性签名：能证明谁签的、签后有没有被改。
证书式数字签名需要 PKI，现场不具备密钥发放条件，因此不做 PKI 承诺。
"""

from __future__ import annotations

import hashlib
import hmac
import json


def canonical_json(payload: object) -> str:
    """按键名排序序列化，作为签名与摘要的规范输入。"""

    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest_content(payload: object) -> str:
    """计算内容的 SHA-256 十六进制摘要。"""

    blob = canonical_json(payload).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def sign_content(signer: str, content_digest: str, secret: str) -> str:
    """对"签名人 + 内容摘要"做 HMAC-SHA256，返回十六进制签名。"""

    if not signer:
        raise ValueError("签名人不能为空")
    if not secret:
        raise ValueError("签名密钥不能为空")
    message = f"{signer}|{content_digest}".encode("utf-8")
    return hmac.new(secret.encode("utf-8"), message, hashlib.sha256).hexdigest()


def verify_signature(signer: str, content_digest: str, secret: str, signature: str) -> bool:
    """恒定时间比对签名是否匹配。"""

    if not signature or not secret:
        return False
    expected = sign_content(signer, content_digest, secret)
    return hmac.compare_digest(expected, signature)
