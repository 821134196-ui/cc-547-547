"""种子数据：构造一个「部分验收」样例工程。

覆盖：合同价调整（留版本）、项目经理更换（留任期）、整改部分通过/待确认/
被打回新问题、质保期返修与未结索赔、回执延迟争议、进度款与照片依据。
"""
from __future__ import annotations

import shutil
from datetime import datetime
from pathlib import Path

from .adapters import MockPaymentGateway
from .db import connect
from .money import yuan_to_cents
from .service import SettlementService

CUSTOMER = ("u_customer", "沈雅琴", "customer")
MANAGER_OLD = ("u_manager_old", "周建国", "manager")
MANAGER_NEW = ("u_manager_new", "林卫民", "manager")
CONTRACT_ID = "CT-2026-0315"


class _Clock:
    def __init__(self, dt: datetime) -> None:
        self.dt = dt

    def __call__(self) -> datetime:
        return self.dt

    def set(self, dt: datetime) -> None:
        self.dt = dt


def _photo_svg(title: str, tag: str, location: str, when: str, accent: bool = True) -> bytes:
    """生成一张占位现场照片（SVG），模拟本地存储的整改实拍。"""
    orange = "#e8611a" if accent else "#6b7280"
    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="640" height="420" viewBox="0 0 640 420">
  <rect width="640" height="420" fill="#e9e7e2"/>
  <rect x="0" y="300" width="640" height="120" fill="#d6d2c9"/>
  <rect x="60" y="70" width="380" height="230" fill="#f7f6f3" stroke="#9ca3af" stroke-width="2"/>
  <line x1="60" y1="180" x2="440" y2="180" stroke="#c7c3ba" stroke-width="2"/>
  <line x1="250" y1="70" x2="250" y2="300" stroke="#c7c3ba" stroke-width="2"/>
  <rect x="470" y="90" width="120" height="210" fill="#ddd9d0" stroke="#9ca3af" stroke-width="2"/>
  <circle cx="530" cy="130" r="10" fill="{orange}"/>
  <rect x="24" y="24" width="126" height="34" rx="4" fill="{orange}"/>
  <text x="36" y="47" font-family="sans-serif" font-size="18" fill="#ffffff">{tag}</text>
  <text x="24" y="350" font-family="sans-serif" font-size="22" font-weight="bold" fill="#1f2933">{title}</text>
  <text x="24" y="378" font-family="sans-serif" font-size="15" fill="#4b5563">{location} · 拍摄 {when}</text>
</svg>""".encode("utf-8")


def seed(data_dir: str | Path = "data", *, force: bool = True) -> Path:
    data_dir = Path(data_dir)
    db_path = data_dir / "renovation.db"
    photo_dir = data_dir / "photos"
    if force:
        for suffix in ("", "-wal", "-shm"):
            p = Path(str(db_path) + suffix)
            if p.exists():
                p.unlink()
        if photo_dir.exists():
            shutil.rmtree(photo_dir)

    con = connect(db_path)
    clock = _Clock(datetime(2026, 3, 15, 9, 0))
    gateway = MockPaymentGateway()
    svc = SettlementService(con, gateway=gateway, photo_dir=photo_dir, clock=clock)

    con.executemany(
        "INSERT INTO users(id,name,role) VALUES (?,?,?)",
        [CUSTOMER, MANAGER_OLD, MANAGER_NEW])
    con.commit()

    # ---- 合同 v1：50 万，质保金固定 2.5 万，质保期至 2028-03-15 ----
    clock.set(datetime(2026, 3, 15, 9, 30))
    con.execute(
        "INSERT INTO contracts(id,title,customer_id,manager_id,current_version,"
        "retention_cents,warranty_end,created_at) VALUES (?,?,?,?,?,?,?,?)",
        (CONTRACT_ID, "景和苑 3 栋 1202 住宅精装修", CUSTOMER[0], MANAGER_OLD[0], 1,
         yuan_to_cents("25000"), "2028-03-15", clock().isoformat(timespec="seconds")))
    con.execute(
        "INSERT INTO contract_versions(contract_id,version_no,amount_cents,change_reason,"
        "created_by,created_at) VALUES (?,?,?,?,?,?)",
        (CONTRACT_ID, 1, yuan_to_cents("500000"), "合同签订价", CUSTOMER[0],
         clock().isoformat(timespec="seconds")))
    con.execute(
        "INSERT INTO pm_tenures(contract_id,manager_id,seq,started_at) VALUES (?,?,1,?)",
        (CONTRACT_ID, MANAGER_OLD[0], clock().isoformat(timespec="seconds")))
    con.commit()

    # ---- 合同价调整：厨房升级 +18,000，生成 v2，旧价保留 ----
    clock.set(datetime(2026, 5, 2, 14, 0))
    svc.adjust_price(CUSTOMER[0], CONTRACT_ID, yuan_to_cents("518000"),
                     "厨房橱柜升级与水槽改位，双方确认增项 18,000 元")

    # ---- 更换项目经理：旧经理调岗，留任期与原因 ----
    clock.set(datetime(2026, 6, 10, 10, 0))
    svc.replace_manager(CUSTOMER[0], CONTRACT_ID, MANAGER_NEW[0],
                        "原项目经理周建国调岗，由林卫民接任，任期与整改责任连续")

    # ---- 完成量（仅经理录入） ----
    clock.set(datetime(2026, 6, 12, 16, 0))
    svc.record_progress(MANAGER_NEW[0], CONTRACT_ID, "水电管线敷设", 1.0, "项",
                        yuan_to_cents("86000"))
    clock.set(datetime(2026, 7, 20, 16, 0))
    svc.record_progress(MANAGER_NEW[0], CONTRACT_ID, "墙地砖铺贴", 96.0, "㎡",
                        yuan_to_cents("152000"))
    clock.set(datetime(2026, 8, 28, 16, 0))
    svc.record_progress(MANAGER_NEW[0], CONTRACT_ID, "木作与油漆", 1.0, "项",
                        yuan_to_cents("174500"))

    # ---- 进度款（模拟网关出账；第二笔回执延迟并立争议） ----
    clock.set(datetime(2026, 4, 10, 11, 0))
    p1 = svc.book_progress_payment(
        MANAGER_OLD[0], CONTRACT_ID, yuan_to_cents("155400"),
        "签约首付款 30%（合同 v1）", idem_key="progress:CT-2026-0315:p1")
    clock.set(datetime(2026, 7, 22, 11, 0))
    p2 = svc.book_progress_payment(
        MANAGER_NEW[0], CONTRACT_ID, yuan_to_cents("207200"),
        "水电隐蔽工程验收后进度款 40%", idem_key="progress:CT-2026-0315:p2")
    clock.set(datetime(2026, 8, 30, 11, 0))
    p3 = svc.book_progress_payment(
        MANAGER_NEW[0], CONTRACT_ID, yuan_to_cents("117900"),
        "木作油漆完工进度款", idem_key="progress:CT-2026-0315:p3")

    # p1 回执已到；p2 银行回执延迟
    clock.set(datetime(2026, 4, 11, 9, 30))
    gateway.deliver_receipt(p1["id"])
    svc.refresh_receipt(p1["id"])
    clock.set(datetime(2026, 8, 12, 9, 0))
    svc.raise_dispute(
        CUSTOMER[0], CONTRACT_ID, "进度款银行回执延迟",
        "207,200 元已于 7/22 出账，银行回单至 8/12 仍未送达，暂按网关流水挂账，待纸质回单补齐。",
        ref_type="payment", ref_id=p2["id"])

    clock.set(datetime(2026, 9, 1, 10, 0))
    gateway.deliver_receipt(p3["id"])
    svc.refresh_receipt(p3["id"])

    # ---- 竣工验收整改清单（2026-09-20 验收） ----
    clock.set(datetime(2026, 9, 20, 15, 0))
    r01 = svc.add_rectification(MANAGER_NEW[0], CONTRACT_ID, "R-01",
                                "主卧墙面裂纹修补", "主卧东墙",
                                "东墙窗角处约 40cm 斜向裂纹，需开槽贴网后重批腻子",
                                yuan_to_cents("3000"))
    r02 = svc.add_rectification(MANAGER_NEW[0], CONTRACT_ID, "R-02",
                                "厨房柜门铰链调整", "厨房西侧橱柜",
                                "上柜两扇门铰链松动、开合异响，需更换铰链并调平",
                                yuan_to_cents("1500"))
    r03 = svc.add_rectification(MANAGER_NEW[0], CONTRACT_ID, "R-03",
                                "次卫地漏排水整改", "次卫淋浴区",
                                "地漏抬高处积水约 5mm 排不净，需局部找坡重做",
                                yuan_to_cents("4500"))
    r04 = svc.add_rectification(MANAGER_NEW[0], CONTRACT_ID, "R-04",
                                "阳台插座缺地线", "生活阳台",
                                "洗衣机位插座无地线，需穿管补接并复测绝缘",
                                yuan_to_cents("2500"))
    r05 = svc.add_rectification(MANAGER_NEW[0], CONTRACT_ID, "R-05",
                                "入户门框划痕补漆", "入户门",
                                "门框右侧 20cm 划痕露底，需局部补漆抛光",
                                yuan_to_cents("1000"))

    # R-01 提交+客户签认通过
    clock.set(datetime(2026, 9, 25, 10, 30))
    s01 = svc.submit_rectification(MANAGER_NEW[0], r01, "已开槽贴抗裂网重批，裂纹处满贴玻纤布", [
        ("整改前.svg", _photo_svg("主卧东墙裂纹（整改前）", "整改前", "主卧东墙", "09-25 09:50", False),
         "窗角斜向裂纹约 40cm"),
        ("整改后.svg", _photo_svg("主卧东墙修补（整改后）", "整改后", "主卧东墙", "09-25 10:20"),
         "贴网重批后墙面平整"),
    ])
    clock.set(datetime(2026, 9, 25, 18, 0))
    svc.decide_submission(CUSTOMER[0], s01, True, "现场复验平整无色差，同意通过")

    # R-02 提交+通过
    clock.set(datetime(2026, 9, 26, 9, 40))
    s02 = svc.submit_rectification(MANAGER_NEW[0], r02, "更换 4 只缓冲铰链并调平柜门", [
        ("整改后.svg", _photo_svg("橱柜铰链更换（整改后）", "整改后", "厨房西柜", "09-26 09:30"),
         "新铰链开合无异响"),
    ])
    clock.set(datetime(2026, 9, 26, 16, 30))
    svc.decide_submission(CUSTOMER[0], s02, True, "开合顺畅，门缝均匀，通过")

    # R-03 已整改提交，待客户确认（橙色待处理）
    clock.set(datetime(2026, 9, 28, 14, 0))
    svc.submit_rectification(MANAGER_NEW[0], r03, "局部拆除地砖找坡 1.5%，重做防水并闭水 24h", [
        ("找坡施工.svg", _photo_svg("次卫地漏找坡重做", "整改后", "次卫淋浴区", "09-28 13:20"),
         "坡向地漏，积水排净"),
        ("闭水记录.svg", _photo_svg("闭水试验记录", "验收依据", "次卫", "09-28 18:00"),
         "闭水 24 小时楼下无渗漏"),
    ])

    # R-04 第一次提交被客户打回，提出新问题；等待返工
    clock.set(datetime(2026, 9, 27, 11, 0))
    s04 = svc.submit_rectification(MANAGER_NEW[0], r04, "已补接地线，万用表测通", [
        ("接线照片.svg", _photo_svg("阳台插座补接地线", "待复验", "生活阳台", "09-27 10:40"),
         "黄绿地线已穿至箱内"),
    ])
    clock.set(datetime(2026, 9, 28, 9, 0))
    svc.decide_submission(
        CUSTOMER[0], s04, False,
        "新问题：地线虽通，但穿线未走线管、与燃气管间距不足 5cm，须套管固定并加漏电保护后复验")

    # ---- 质保期内返修：一笔已结清，一笔未结索赔（阻塞质保金） ----
    clock.set(datetime(2026, 9, 29, 9, 0))
    w01 = svc.add_rectification(MANAGER_NEW[0], CONTRACT_ID, "W-01",
                                "主卫花洒接口渗水返修", "主卫淋浴区",
                                "入住后花洒暗装接口处缓慢渗水", 0, kind="warranty")
    claim1 = svc.open_warranty_claim(CUSTOMER[0], CONTRACT_ID,
                                     "主卫花洒接口渗水", "暗装接口垫圈老化渗水，要求免费返修",
                                     rect_id=w01)
    clock.set(datetime(2026, 9, 29, 15, 0))
    sw01 = svc.submit_rectification(MANAGER_NEW[0], w01, "更换铜垫并做 0.8MPa 打压 30 分钟", [
        ("返修后.svg", _photo_svg("花洒接口返修（质保）", "质保返修", "主卫", "09-29 14:30"),
         "打压 30 分钟压降为 0"),
    ])
    clock.set(datetime(2026, 9, 30, 10, 0))
    svc.decide_submission(CUSTOMER[0], sw01, True, "打压合格，墙面无返潮，返修通过")
    svc.resolve_claim(MANAGER_NEW[0], claim1, "接口重接打压合格，客户确认恢复")

    clock.set(datetime(2026, 10, 1, 20, 0))
    svc.open_warranty_claim(CUSTOMER[0], CONTRACT_ID,
                            "客卧窗台暴雨后渗水",
                            "10/1 暴雨后客卧窗台内侧有约 15cm 湿痕，疑外窗打胶不密实，待雨后安排注浆")

    # ---- 尾款：经理已就 R-01/R-02 发起释放（¥4,500），等待客户签放 ----
    clock.set(datetime(2026, 10, 1, 21, 0))
    svc.release_payment(MANAGER_NEW[0], CONTRACT_ID, "final", [r01, r02])

    clock.set(datetime(2026, 10, 2, 10, 0))
    con.close()
    return db_path


if __name__ == "__main__":
    path = seed()
    print(f"已写入样例数据库：{path}")
