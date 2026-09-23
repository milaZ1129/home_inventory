import argparse
import getpass
import sys
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_DIR))

from security import write_security_config  # noqa: E402


def main():
    parser = argparse.ArgumentParser(
        description="设置家庭物品系统的登录用户名和密码"
    )
    parser.add_argument("--username", default="family")
    parser.add_argument(
        "--allow-short",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    args = parser.parse_args()

    password = getpass.getpass("新密码：")
    confirmation = getpass.getpass("再次输入：")

    if password != confirmation:
        raise SystemExit("两次输入的密码不一致")
    if len(password) < 8 and not args.allow_short:
        raise SystemExit("密码至少需要 8 个字符")

    config_path = write_security_config(args.username, password)
    print(f"安全配置已更新：{config_path}")


if __name__ == "__main__":
    main()
