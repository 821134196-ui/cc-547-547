"""HTTP 服务：JSON API + 静态页面 + 本地照片，纯标准库实现。

角色切换通过请求头 X-User-Id（模拟登录），服务端按角色做权限校验。
"""
from __future__ import annotations

import json
import mimetypes
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from .adapters import PaymentAdapter, SignatureAdapter
from .db import Database
from .errors import Conflict, DomainError, NotFound, PermissionDenied
from .seed import seed_all
from .services import Service

STATIC_DIR = Path(__file__).parent / "static"


def build_service(db_path: str, photos_dir: str, reseed: bool = False):
    db = Database(db_path)
    svc = Service(db, photos_dir, PaymentAdapter(), SignatureAdapter())
    with db.connect() as c:
        n = c.execute("SELECT COUNT(*) n FROM projects").fetchone()["n"]
    if reseed or n == 0:
        seed_all(svc)
    return svc


class Handler(BaseHTTPRequestHandler):
    svc: Service = None
    project_id: int = 1

    def log_message(self, fmt, *args):
        pass  # 安静启动；需要时可打开

    # ------------------------------------------------------------- response

    def _json(self, obj, status=200):
        body = json.dumps(obj, ensure_ascii=False, default=str).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _file(self, path: Path):
        if not path.is_file():
            self._json({"error": "not found"}, 404)
            return
        body = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type",
                         mimetypes.guess_type(str(path))[0] or "application/octet-stream")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict:
        n = int(self.headers.get("Content-Length", 0))
        if not n:
            return {}
        return json.loads(self.rfile.read(n).decode())

    def _actor(self) -> int:
        uid = self.headers.get("X-User-Id")
        if not uid:
            raise PermissionDenied("缺少 X-User-Id（请选择当前操作人）")
        return int(uid)

    def _handle_error(self, e):
        if isinstance(e, PermissionDenied):
            self._json({"error": str(e)}, 403)
        elif isinstance(e, NotFound):
            self._json({"error": str(e)}, 404)
        elif isinstance(e, Conflict):
            self._json({"error": str(e)}, 409)
        elif isinstance(e, DomainError):
            self._json({"error": str(e)}, 422)
        else:
            self._json({"error": f"服务器错误: {e}"}, 500)

    # ----------------------------------------------------------------- GET

    def do_GET(self):
        try:
            url = urlparse(self.path)
            p = url.path
            if p in ("/", "/index.html"):
                return self._file(STATIC_DIR / "index.html")
            if p.startswith("/static/"):
                return self._file(STATIC_DIR / p[len("/static/"):])
            if p == "/api/state":
                return self._json(self._state())
            if p.startswith("/api/photos/"):
                rel = p[len("/api/photos/"):]
                safe = Path(rel).name  # 仅 单级/文件名，防穿越
                # 路径形如 <rect_id>/<file>
                target = (self.svc.photos_dir / rel).resolve()
                root = self.svc.photos_dir.resolve()
                if root not in target.parents or not target.is_file():
                    return self._json({"error": "not found"}, 404)
                return self._file(target)
            self._json({"error": "not found"}, 404)
        except Exception as e:  # noqa: BLE001
            self._handle_error(e)

    def _state(self):
        pid = self.project_id
        svc = self.svc
        with svc.db.connect() as c:
            users = [dict(u) for u in c.execute(
                "SELECT id,name,role FROM users ORDER BY role,id")]
            payments = [dict(x) for x in c.execute(
                "SELECT p.*,r.kind req_kind FROM payments p"
                " JOIN payment_requests r ON r.id=p.request_id"
                " WHERE r.project_id=? ORDER BY p.id", (pid,))]
            adjustments = [dict(x) for x in c.execute(
                "SELECT a.*,u.name creator_name FROM contract_adjustments a"
                " JOIN users u ON u.id=a.created_by WHERE a.project_id=? ORDER BY a.id", (pid,))]
            mgr_hist = [dict(x) for x in c.execute(
                "SELECT h.*,o.name old_name,n.name new_name FROM manager_history h"
                " LEFT JOIN users o ON o.id=h.old_manager_id JOIN users n ON n.id=h.new_manager_id"
                " WHERE h.project_id=? ORDER BY h.id", (pid,))]
            disputes = [dict(x) for x in c.execute(
                "SELECT d.*,u.name raiser_name FROM disputes d JOIN users u ON u.id=d.raised_by"
                " WHERE d.project_id=? ORDER BY d.id", (pid,))]
            repairs = [dict(x) for x in c.execute(
                "SELECT w.*,u.name confirmer_name FROM warranty_repairs w"
                " LEFT JOIN users u ON u.id=w.confirmed_by WHERE w.project_id=? ORDER BY w.id", (pid,))]
            completions = [dict(x) for x in c.execute(
                "SELECT wc.*,u.name enterer_name FROM work_completions wc"
                " JOIN users u ON u.id=wc.entered_by WHERE wc.project_id=? ORDER BY wc.id", (pid,))]
            events = [dict(x) for x in c.execute(
                "SELECT e.*,u.name actor_name FROM event_log e"
                " LEFT JOIN users u ON u.id=e.actor_id ORDER BY e.id")]
        return {
            "users": users,
            "settlement": svc.settlement(pid),
            "rectifications": svc.rectifications(pid),
            "payments": payments,
            "adjustments": adjustments,
            "manager_history": mgr_hist,
            "disputes": disputes,
            "warranty_repairs": repairs,
            "completions": completions,
            "events": events,
        }

    # ---------------------------------------------------------------- POST

    def do_POST(self):
        try:
            url = urlparse(self.path)
            p = url.path.rstrip("/")
            body = self._read_json()
            actor = self._actor()
            svc = self.svc
            pid = self.project_id

            if p == "/api/rects/submit":
                # 演示环境：用内置图案模拟“上传整改照片”
                from .pngutil import make_paint_png
                import base64
                pattern = body.get("pattern", "grid")
                data = base64.b64decode(body["photo"]) if body.get("photo") \
                    else make_paint_png(pattern=pattern)
                svc.submit_rect(body["rect_id"], actor, [(data, body.get("caption", "整改照片"))])
                return self._json({"ok": True})

            if p == "/api/rects/sign":
                return self._json(svc.sign_rect(body["rect_id"], actor))

            if p == "/api/issues":
                from .pngutil import make_paint_png
                data = make_paint_png(pattern=body.get("pattern", "dots"))
                rid = svc.add_customer_issue(pid, actor, body["parent_id"],
                                             body["title"], data,
                                             body.get("caption", ""))
                return self._json({"id": rid})

            if p == "/api/payments/tail":
                return self._json(svc.release_tail(
                    pid, actor, body["rect_ids"],
                    delay_receipt=bool(body.get("delay_receipt"))))

            if p == "/api/payments/receipt":
                action = body.get("action", "confirm")
                if action == "replay":
                    return self._json(svc.replay_receipt(body["request_id"]))
                return self._json(svc.confirm_receipt(body["request_id"]))

            if p == "/api/payments/retention":
                return self._json(svc.release_retention(pid, actor))

            if p == "/api/warranty/open":
                rid = svc.open_warranty_repair(pid, actor, body["title"],
                                               body.get("basis", ""))
                return self._json({"id": rid})
            if p == "/api/warranty/repair":
                svc.repair_done(body["id"], actor)
                return self._json({"ok": True})
            if p == "/api/warranty/confirm":
                svc.confirm_repair(body["id"], actor,
                                   as_claim=bool(body.get("as_claim")),
                                   cost_yuan=body.get("cost", 0))
                return self._json({"ok": True})

            if p == "/api/disputes":
                rid = svc.raise_dispute(pid, actor, body["subject"],
                                        body.get("detail", ""), body["basis"])
                return self._json({"id": rid})
            if p == "/api/disputes/resolve":
                svc.resolve_dispute(body["id"], actor)
                return self._json({"ok": True})

            if p == "/api/contract/adjust":
                rid = svc.adjust_contract(pid, actor, body["delta"],
                                          body["reason"], body["basis"])
                return self._json({"id": rid})

            if p == "/api/completions":
                # 明确验证：客户代录完成量会被 403 拒绝
                rid = svc.enter_completion(pid, actor, body["item_name"],
                                           float(body["quantity"]), body["unit"])
                return self._json({"id": rid})

            self._json({"error": "not found"}, 404)
        except Exception as e:  # noqa: BLE001
            self._handle_error(e)


def serve(db_path: str, photos_dir: str, host: str = "127.0.0.1",
          port: int = 8000, reseed: bool = False):
    Handler.svc = build_service(db_path, photos_dir, reseed=reseed)
    httpd = ThreadingHTTPServer((host, port), Handler)
    print(f"装修工程结算系统已启动: http://{host}:{port}")
    return httpd
