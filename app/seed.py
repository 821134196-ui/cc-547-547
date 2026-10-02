"""种子数据：构造一个“竣工验收后、部分整改通过”的真实样例项目。

样例包含：
- 合同价调整（增项，留依据）、项目经理更换（留依据）
- 部分整改通过并已申请尾款（其中一笔回执延迟在途）
- 已通过整改后客户追加的新问题
- 待复验、待整改项；质保期内返修；开放争议
"""
from __future__ import annotations

from datetime import date, timedelta

from .pngutil import make_paint_png

# 合同 192,000 + 增项 8,000 = 200,000；质保金 5% = 10,000；
# 非质保付款额度 190,000
# 进度款 130,000；已付/在途尾款 10,000（回执延迟）；
# 已签认待放款 15,000；新问题挂起 20,000；待签认 8,000；待整改 5,000；尾差 2,000


def seed_all(svc, *, today: date | None = None) -> dict:
    today = today or date.today()
    ids = {}

    customer = svc.create_user("王琳（客户）", "customer")
    mgr_old = svc.create_user("赵强（原项目经理）", "manager")
    mgr_new = svc.create_user("陈晨（项目经理）", "manager")
    ids.update(customer=customer, mgr_old=mgr_old, mgr_new=mgr_new)

    acceptance = (today - timedelta(days=60)).isoformat()
    pid = svc.create_project("幸福里 3-2-1801 家装工程", mgr_old,
                             "192000", 5.0, warranty_days=180,
                             acceptance_iso=acceptance)
    ids["project"] = pid

    # 合同价调整：客餐厅增加吊顶 8,000，有签证单依据
    svc.adjust_contract(pid, mgr_old, "8000",
                        "客餐厅增加边吊及回光灯槽",
                        "工程签证单 GC-2026-014，客户现场签字照片")

    # 项目经理更换
    svc.replace_manager(pid, customer, mgr_new,
                        "原项目经理赵强内部调岗",
                        "《项目经理更换通知》HR-2026-031 及客户确认微信记录")

    # 项目经理录入完成量（客户不可代录）
    svc.enter_completion(pid, mgr_new, "水电隐蔽工程", 100, "%")
    svc.enter_completion(pid, mgr_new, "墙地砖铺贴", 128, "㎡")
    svc.enter_completion(pid, mgr_new, "乳胶漆", 320, "㎡")

    # 进度款两笔，合计 130,000
    svc.pay_progress(pid, mgr_new, "80000", "第一次进度款（水电+泥木验收）")
    svc.pay_progress(pid, mgr_new, "50000", "第二次进度款（油漆完工）")

    # ---- R1 已通过并放款（回执延迟，在途）----
    r1 = svc.create_rect(pid, mgr_new, "阳台地砖找平返工", "10000",
                         "阳台积水倒灌，重新找平铺贴")
    svc.submit_rect(r1, mgr_new, [
        (make_paint_png(pattern="grid", base=(214, 210, 200)), "返工后找平照片"),
        (make_paint_png(pattern="band"), "闭水试验 48 小时照片"),
    ])
    svc.sign_rect(r1, customer)
    res = svc.release_tail(pid, customer, [r1], delay_receipt=True)
    ids["r1_payment"] = res["request_id"]

    # ---- R2 已提交复验、待客户签认（15,000，可释放）----
    r2 = svc.create_rect(pid, mgr_new, "主卧墙面开裂修复", "15000",
                         "主卧东墙乳胶漆裂纹，挂网重做")
    svc.submit_rect(r2, mgr_new, [
        (make_paint_png(pattern="crack", base=(235, 232, 225)), "裂缝处贴网照片"),
        (make_paint_png(pattern="grid", base=(238, 235, 228)), "面漆完成后照片"),
    ])
    svc.sign_rect(r2, customer)
    ids["r2"] = r2

    # ---- R2b 已提交复验，等待客户签认（8,000）----
    r2b = svc.create_rect(pid, mgr_new, "书房插座回路漏保更换", "8000",
                          "漏保误跳，更换 A 型漏保")
    svc.submit_rect(r2b, mgr_new, [
        (make_paint_png(pattern="dots", base=(232, 233, 235)), "新漏保安装照片"),
    ])
    ids["r2b"] = r2b

    # ---- R3 已整改提交，客户复验部分通过，提出新问题 ----
    r3 = svc.create_rect(pid, mgr_new, "卫生间门口渗水", "20000",
                         "门槛石下方渗水，重做防水")
    svc.submit_rect(r3, mgr_new, [
        (make_paint_png(pattern="band", base=(205, 214, 220)), "门槛石防水翻边照片"),
    ])
    svc.sign_rect(r3, customer)
    # 客户确认后又发现新问题（挂为子项，未验收 => 不计入可付金额）
    svc.add_customer_issue(
        pid, customer, r3, "卫生间门套底部仍有轻微返潮",
        photo_bytes=make_paint_png(pattern="dots", base=(210, 205, 196)),
        caption="雨后第三天门套底部返潮照片")
    ids["r3"] = r3

    # ---- R4 尚未整改 ----
    r4 = svc.create_rect(pid, mgr_new, "橱柜门板缝隙不均", "5000",
                         "吊柜三扇门缝差 2mm，需厂家调试")
    ids["r4"] = r4

    # ---- 质保期内返修（未结）----
    wr = svc.open_warranty_repair(pid, customer, "客厅吊顶转角处返碱",
                                  "客户 9 月 28 日报修，现场照片存档")
    ids["warranty"] = wr

    # ---- 争议 ----
    svc.raise_dispute(
        pid, customer, "卫生间新返潮问题责任边界",
        "客户认为属上次防水施工范围；项目经理认为系入户淋浴水汽所致，待第三方上门判定。",
        "复验照片、聊天记录 2026-09-30、原施工合同第 8.2 条")

    return ids
