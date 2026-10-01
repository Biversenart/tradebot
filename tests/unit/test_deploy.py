"""Static guarantees of the deployment files (kill-switch, no direct egress, no secrets)."""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
DEPLOY = ROOT / "deploy"


def compose() -> dict[str, Any]:
    data: dict[str, Any] = yaml.safe_load((ROOT / "docker-compose.yml").read_text("utf-8"))
    return data


def test_bot_shares_wireguard_network_namespace() -> None:
    bot = compose()["services"]["bot"]
    assert bot["network_mode"] == "service:wireguard"
    assert "ports" not in bot and "networks" not in bot
    assert bot["depends_on"]["wireguard"]["condition"] == "service_healthy"


def test_wireguard_service_has_killswitch() -> None:
    wg = compose()["services"]["wireguard"]
    assert "NET_ADMIN" in wg["cap_add"]
    mounts = " ".join(wg["volumes"])
    assert "killswitch.sh:/custom-cont-init.d/" in mounts
    assert wg["sysctls"]["net.ipv6.conf.all.disable_ipv6"] == "1"
    for port in wg.get("ports", []):
        assert port.startswith("127.0.0.1:"), "panel yalnızca localhost'a açılmalı"


def test_killswitch_script_is_fail_closed() -> None:
    text = (DEPLOY / "local" / "killswitch.sh").read_text("utf-8")
    assert "iptables -P OUTPUT DROP" in text
    assert "-o wg0 -j ACCEPT" in text
    assert "ip6tables -P OUTPUT DROP" in text
    # policy DROP is set before flushing -> no open window
    assert text.index("iptables -P OUTPUT DROP") < text.index("iptables -F OUTPUT")
    # nothing else allowed to the internet besides wg0, local net and the VPS endpoint
    accepts = [ln for ln in text.splitlines() if ln.startswith("iptables -A OUTPUT")]
    assert len(accepts) == 5


def test_postgres_reachable_by_static_ip_inside_killswitch_net() -> None:
    c = compose()
    subnet = c["networks"]["botnet"]["ipam"]["config"][0]["subnet"]
    assert c["services"]["wireguard"]["environment"]["KILLSWITCH_LOCAL_NET"] == subnet
    pg_ip = c["services"]["postgres"]["networks"]["botnet"]["ipv4_address"]
    assert pg_ip in c["services"]["bot"]["environment"]["DATABASE_URL"]
    assert "ports" not in c["services"]["postgres"]


def test_client_template_routes_everything_through_tunnel() -> None:
    text = (DEPLOY / "wireguard" / "client" / "wg0.conf.template").read_text("utf-8")
    assert re.search(r"^AllowedIPs = 0\.0\.0\.0/0$", text, re.M)
    assert re.search(r"^DNS = ", text, re.M)
    assert "PersistentKeepalive" in text


def test_dante_listens_only_on_tunnel() -> None:
    text = (DEPLOY / "dante" / "danted.conf.template").read_text("utf-8")
    assert "internal: __SERVER_TUNNEL_IP__" in text
    assert "socksmethod: username" in text
    assert "client block" in text and "socks block" in text


def test_ufw_denies_by_default() -> None:
    text = (DEPLOY / "ufw" / "setup_ufw.sh").read_text("utf-8")
    assert "ufw default deny incoming" in text
    assert "ufw default deny routed" in text
    # SSH rule is added before enabling the firewall
    assert text.index("22/tcp") < text.index("ufw --force enable")


def test_no_real_keys_in_templates() -> None:
    key = re.compile(r"[A-Za-z0-9+/]{43}=")  # WireGuard base64 key shape
    for f in DEPLOY.rglob("*"):
        if f.is_file():
            assert not key.search(f.read_text("utf-8")), f


def test_wg_confs_are_gitignored() -> None:
    gi = (ROOT / ".gitignore").read_text("utf-8")
    assert "deploy/local/wg_confs/*" in gi


SCRIPTS = sorted(DEPLOY.rglob("*.sh"))


@pytest.mark.parametrize("script", SCRIPTS, ids=lambda p: str(p.relative_to(DEPLOY)))
def test_shell_syntax(script: Path) -> None:
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("bash yok")
    subprocess.run([bash, "-n", str(script)], check=True)  # noqa: S603
