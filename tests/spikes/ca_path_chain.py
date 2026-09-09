"""spike：ca_path 候选链在 dev/prod 场景的选择逻辑（与 infra/sandbox.py:13-32 同构）
2026-09-09 chat 500 根因修复验证。运行: python tests/spikes/ca_path_chain.py
"""
import os
from pathlib import Path
import tempfile

tmpdir = Path(tempfile.mkdtemp())
dev_cert = tmpdir / "dev-rootCA.pem"; dev_cert.write_text("DEV-CERT")
prod_cert = tmpdir / "prod-combined.pem"; prod_cert.write_text("PROD-CERT")

def resolve(candidates):
    return next((c for c in candidates if c and os.path.isfile(c)), "")

def run(name, settings_ca, env_cube_ca, env_ssl, base_default):
    old = {k: os.environ.get(k) for k in ("CUBE_CA_PATH", "SSL_CERT_FILE")}
    os.environ.pop("CUBE_CA_PATH", None); os.environ.pop("SSL_CERT_FILE", None)
    if env_cube_ca: os.environ["CUBE_CA_PATH"] = env_cube_ca
    if env_ssl: os.environ["SSL_CERT_FILE"] = env_ssl
    candidates = [settings_ca, os.getenv("CUBE_CA_PATH"), os.getenv("SSL_CERT_FILE"), str(base_default)]
    got = resolve(candidates)
    for k, v in old.items():
        if v is None: os.environ.pop(k, None)
        else: os.environ[k] = v
    ok = "OK " if got else "EMPTY(安全)"
    print(f"[{ok}] {name}: -> {got!r}")
    return got

fails = []
# 场景 1：dev 修复后——settings 命中 .env CUBE_CA_PATH
if not run("dev-修复后(settings命中)", str(dev_cert), None, None, "/nonexistent/x.pem"): fails.append(1)
# 场景 2：prod——compose env 注入 → settings/os.getenv 同值命中
if not run("prod-compose注入", str(prod_cert), str(prod_cert), "/etc/ssl/certs/c.pem", "/nonexistent/x.pem"): fails.append(2)
# 场景 3：无 CUBE_CA_PATH 但有 SSL_CERT_FILE 存在 → 命中 SSL_CERT_FILE
if not run("dev-仅SSL_CERT_FILE", "", None, str(dev_cert), "/nonexistent/x.pem"): fails.append(3)
# 场景 4：全部缺失 → "" 不写 os.environ（防污染）
if run("全部缺失-防污染", "", None, None, "/nonexistent/x.pem"): fails.append(4)
# 场景 5：默认 certs/rootCA.pem 存在 → 命中默认
if not run("默认certs存在", "", None, None, str(dev_cert)): fails.append(5)

print("")
if fails:
    print(f"FAIL: 场景 {fails} 未达预期")
    raise SystemExit(1)
print("ALL PASS: 场景 1/2/3/5 命中真实文件，场景 4 返回空（不污染 os.environ）")
