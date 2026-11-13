"""Key Vault 비밀을 관리 ID로 읽어 환경변수에 넣은 뒤 대상 프로그램을 실행한다.

사용: python kv_launch.py ENV=secret-name [ENV=secret-name ...] -- program args...
비밀 값은 디스크에 쓰지 않고 로그에도 남기지 않는다.
"""
import json
import os
import subprocess
import sys
import time
import urllib.request

VAULT = os.environ.get("AICAF_KEYVAULT", "https://<KEY_VAULT_NAME>.vault.azure.net")


def token():
    req = urllib.request.Request(
        "http://169.254.169.254/metadata/identity/oauth2/token?api-version=2018-02-01&resource=https%3A%2F%2Fvault.azure.net",
        headers={"Metadata": "true"})
    return json.load(urllib.request.urlopen(req, timeout=10))["access_token"]


def secret(name, tok):
    req = urllib.request.Request(f"{VAULT}/secrets/{name}?api-version=7.4", headers={"Authorization": f"Bearer {tok}"})
    return json.load(urllib.request.urlopen(req, timeout=10))["value"]


def main():
    argv = sys.argv[1:]
    sep = argv.index("--")
    pairs, cmd = argv[:sep], argv[sep + 1:]
    env = dict(os.environ)
    for attempt in range(30):
        try:
            tok = token()
            for p in pairs:
                k, _, n = p.partition("=")
                env[k] = secret(n, tok)
            break
        except Exception as e:  # 부팅 직후 IMDS·DNS가 늦을 수 있다
            print(f"kv_launch: retry {attempt}: {type(e).__name__}", file=sys.stderr, flush=True)
            time.sleep(10)
    else:
        sys.exit("kv_launch: secrets unavailable")
    sys.exit(subprocess.call(cmd, env=env))


if __name__ == "__main__":
    main()
