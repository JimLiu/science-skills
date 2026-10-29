"""InferProvider — the shim behind every inference-provider kernel.

An inference provider is a registered model-server endpoint (a localhost
port or a hosted URL). The kernel exists only to give the agent a Python
REPL whose network egress is scoped to that one endpoint; cells call the
server's native HTTP API directly (httpx ships in the helper env). There is
no provider SDK to import and no job lifecycle — submit/harvest stay with
the ssh and byoc families — so the helper-mode ops below all refuse.
"""
from __future__ import annotations

import os
import re
from typing import Any, Callable, Iterable, NoReturn

from operon_compute_provider import ByocError, ExecResult


class InferProvider:



    secret_env_prefixes = ("INFER_", "NVIDIA_")

    token_scrub_regex = re.compile(r"\bnvapi-[A-Za-z0-9_\-]{8,}")

    def __init__(self, *, repl: bool = False):
        self._repl = repl








    _alias_name_re = re.compile(r"(?!LD_|DYLD_|MALLOC_)[A-Z][A-Z0-9_]{0,127}")
    _alias_denylist = frozenset((

        "PATH", "HOME", "USER", "SHELL", "TMPDIR", "LANG", "LC_ALL", "PWD",
        "OLDPWD", "TERM", "HOSTNAME", "DISPLAY", "SSH_AUTH_SOCK",
        "PYTHONPATH", "NODE_PATH", "CONDA_PREFIX", "VIRTUAL_ENV", "BASH_ENV",
        "ENV", "PROMPT_COMMAND", "IFS", "PYTHONSTARTUP", "NODE_OPTIONS",
        "GIT_SSH_COMMAND", "GIT_ASKPASS",


        "PYTHONHOME", "SHELLOPTS", "PS4", "ZDOTDIR", "MAKEFILES",





        "GCONV_PATH", "GLIBC_TUNABLES", "LOCPATH", "GETCONF_DIR",
        "HOSTALIASES", "LOCALDOMAIN", "NIS_PATH", "NLSPATH",
        "RESOLV_HOST_CONF", "RES_OPTIONS", "TZDIR",



        "SSH_ASKPASS", "SSH_ASKPASS_REQUIRE",






        "OPERON_ASKPASS_NONCE", "OPERON_ASKPASS_SOCK",


        "OPERON_TRAMPOLINE_TARGET",



        "OPERON_WIN_CONSOLE_RELAY",

        "SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE",
        "CURL_CA_BUNDLE", "NODE_EXTRA_CA_CERTS",
        "NODE_TLS_REJECT_UNAUTHORIZED",
        "AWS_CA_BUNDLE", "GIT_SSL_CAINFO", "GIT_SSL_NO_VERIFY",
        "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "ALL_PROXY",
    ))

    def apply_auth(self, creds: dict[str, str]) -> None:




        key = creds.get("api_key", "")
        if not key:
            return
        os.environ["INFER_API_KEY"] = key






        alias = creds.get("env_name", "")
        if (isinstance(alias, str) and self._alias_name_re.fullmatch(alias)
                and alias not in self._alias_denylist):
            os.environ[alias] = key

    def import_and_patch(self) -> None:



        return None

    def install_unauth_hook(self, on_expired: Callable[[], NoReturn]) -> None:

        return None



    def _unsupported(self) -> NoReturn:
        raise ByocError(
            "invalid_request",
            "inference providers have no job lifecycle — call the endpoint "
            "directly from the inference kernel instead of submitting a job.",
        )

    def create_sandbox(self, spec: dict[str, Any], install_id: str,
                       tags: dict[str, str] | None = None) -> str:
        self._unsupported()

    def exec(
        self,
        sandbox_id: str,
        argv: list[str],
        *,
        stdin: Iterable[bytes] | None = None,
        env: dict[str, str] | None = None,
        timeout: int | None = None,
    ) -> ExecResult:
        self._unsupported()

    def list_owned(self, install_id: str) -> list[dict[str, Any]]:
        self._unsupported()

    def read_owner(self, sandbox_id: str) -> str | None:
        self._unsupported()

    def terminate(self, sandbox_id: str) -> None:
        self._unsupported()


PROVIDER = InferProvider
