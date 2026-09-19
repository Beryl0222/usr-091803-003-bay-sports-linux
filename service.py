"""湾区赛事协同台账运行入口。

用法：
  python3 service.py --check            基础配置自检
  python3 service.py --port 8000        启动 HTTP 服务（/health 为健康检查）
  python3 service.py --port 8000 --store data.jsonl   事件落盘，重启可回放
"""

import argparse

from baysports.api import App, Handler, SERVICE_ID, SERVICE_NAME, make_server


def health_payload():
    """返回稳定的服务身份信息。"""
    return {"status": "ok", "service": SERVICE_ID, "name": SERVICE_NAME}


def main():
    parser = argparse.ArgumentParser(description=SERVICE_NAME)
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--store", default=None, help="事件 JSONL 落盘路径")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    if args.check:
        assert health_payload()["service"] == SERVICE_ID
        # 装配根可构建，规则常量完整
        app = App()
        assert app.tournament is not None
        print("基础检查通过")
        return
    server = make_server(args.host, args.port, App(store_path=args.store))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
