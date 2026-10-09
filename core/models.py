# -*- coding: utf-8 -*-
"""PromptDeck · 数据模型（第三次迭代 · Django ORM 版）。

────────────────────────────────────────────────────────────────
本次迭代换库：data/prompt_test_DB.db → data/prompt_test_DB_twe.db，
并改按新库的表结构读写。表结构由 migrate_to_twe.py 一次性建好，
因此下面所有模型**全部 managed = False**：
    Django 不建表、不改表、不删表，migrate 也不会碰它们，
    模型在这里只充当「读写用的门面」。
（唯一的一张 Django 自己的表是 django_migrations 记账表。）

表清单与分工
    srv               供应商（平台 / 官方名、base_url、公司名、首页）
    mdl               模型（挂在 srv 下，含计费单价）
    tst_plan          提示词方案主体 —— 对应原型 prompt.html 的「保存方案」
    tst_plan_prompt   方案的提示词明细，逐条按 seq_no 排
    tst_api_key       方案的 API_key（一个方案一把，key_name = 「默认」）
    tst_rec           测试记录（本次只映射，不接业务）
    tst_rec_dtl       测试记录明细（本次只映射，不接业务）
    tst_db_conn       已连接数据库的存档（第四次迭代新增，见下方类注释）

────────────────────────────────────────────────────────────────
「提示词方案」在旧库是 tp 单表，本次拆成三张表，字段对照：

    原型（prompt.html 保存的方案对象）       新库落点
    ────────────────────────────────────    ──────────────────────────────
    name        方案命名                     tst_plan.plan_name      ← 加列
    base        请求地址                     tst_plan.base_url       ← 加列
    server      服务器（平台 / 官方名）       tst_plan.srv_id  → srv.plat_name
    model       模型                         tst_plan.mdl_id  → mdl.mdl_name
    modelType   模型类型                     ────────────────→ mdl.mdl_type
    sys         系统提示词                   tst_plan_prompt(seq_no=0,   role=system)
    user:[…]    用户提示词（多轮 = 多元素）   tst_plan_prompt(seq_no=1..N, role=user)
    multi       多轮会话调试                 ← 由 user 条数 > 1 还原，不单独落库
    apiKey      API_key（明文）              tst_api_key.api_key（key_name=「默认」）
    savedAt     保存时间                     ← 原型里只写不读，不落库

    ⚠️ name / base 两列是新库原本没有的：新库 tst_plan 只有 srv_id + mdl_id，
       装不下「方案命名」与「请求地址」，经确认后由本次迁移补上。
    ⚠️ multi 与 user 数组是同一件事：原型 collectUserPrompts() 里多轮开=收集
       第 1 轮 + 追加轮，故 len(user) > 1 ⇔ multi 打开，可用条数无损还原。
"""
from django.db import models


# ================================================================== 基础数据
class Srv(models.Model):
    """供应商表（结构由迁移脚本管理，Django 不碰）。"""

    id = models.AutoField(primary_key=True)
    plat_name = models.TextField()                 # 平台 / 官方名
    base_url = models.TextField(null=True)         # api 网址（base_url）
    comp_name = models.TextField(null=True)        # 公司名
    home_url = models.TextField(null=True)         # 首页

    class Meta:
        managed = False
        db_table = "srv"
        ordering = ["id"]

    def __str__(self):
        return self.plat_name or ""


class Mdl(models.Model):
    """模型表（结构由迁移脚本管理，Django 不碰）。

    in_price / out_price 在新库里是 **TEXT**：计费单价按「用户输入的原文」保存，
    不四舍五入、小数位数原样保留（上一版踩过的坑：DECIMAL 会把 18.00 读成 18，
    本次迁移专门把新库原本的 DECIMAL(10,6) 改成了 TEXT）。
    """

    id = models.AutoField(primary_key=True)
    srv_id = models.IntegerField()                 # 关联 srv.id（ON DELETE CASCADE）
    mdl_name = models.TextField()                  # 模型名
    mdl_type = models.TextField(null=True)         # 类型
    in_price = models.TextField(null=True)         # 输入 token 计费（原文）
    out_price = models.TextField(null=True)        # 输出 token 计费（原文）
    unit = models.TextField(null=True)             # 单位
    url = models.TextField(null=True)              # 网址

    class Meta:
        managed = False
        db_table = "mdl"
        ordering = ["id"]

    def __str__(self):
        return self.mdl_name or ""


# ================================================================== 提示词方案
class TstPlan(models.Model):
    """提示词方案主体（新库的 tst_plan，本次迭代的主角）。

    一条记录 = 原型 prompt.html 点「保存」写下的那一份方案。
    方案的名称与请求地址直接落在本表（plan_name / base_url），
    供应商与模型走外键指向 srv / mdl。
    """

    id = models.AutoField(primary_key=True)
    plan_name = models.TextField(null=True, blank=True)   # 方案命名（加列）
    base_url = models.TextField(null=True, blank=True)    # 请求地址（加列）
    srv_id = models.IntegerField()                        # → srv.id
    mdl_id = models.IntegerField()                        # → mdl.id

    class Meta:
        managed = False
        db_table = "tst_plan"
        ordering = ["id"]

    def __str__(self):
        return self.plan_name or ""


class TstPlanPrompt(models.Model):
    """方案的提示词明细（新库的 tst_plan_prompt）。

    一个方案 N 条，靠 seq_no 排序：
        seq_no = 0        → 系统提示词（prompt_role = "system"）
        seq_no = 1..N     → 用户提示词第 N 轮（prompt_role = "user"）
    """

    id = models.AutoField(primary_key=True)
    plan_id = models.IntegerField()                # → tst_plan.id（ON DELETE CASCADE）
    seq_no = models.IntegerField()                 # 排序号：0 = 系统提示词
    prompt_role = models.TextField()               # system / user
    prompt_content = models.TextField()            # 提示词正文

    class Meta:
        managed = False
        db_table = "tst_plan_prompt"
        ordering = ["plan_id", "seq_no"]

    def __str__(self):
        return "%s#%s %s" % (self.plan_id, self.seq_no, self.prompt_role)


class TstApiKey(models.Model):
    """方案的 API_key（新库的 tst_api_key）。

    原型里一份方案只带一把 key（构造页那一个输入框），迁移过来的记录
    key_name 统一为「默认」、is_active = 1。表结构本身支持一个方案多把 key，
    本次不做多 key 的界面。

    ⚠️ 第二次迭代：plan_id 放宽为**可空**。
       原因是「提示词测试」页的语义一致性判定要用一把**判定模型的密钥**
       （key_name = 'AI判定（DeepSeek）'），它不属于任何一个提示词方案，
       原来 NOT NULL 的 plan_id 装不下它。方案自己的密钥（key_name='默认'）
       仍按 plan_id 归属，读写口径一个字没变。
       结构变更由 setup_judge_key.py 一次性完成（幂等）。
    """

    id = models.AutoField(primary_key=True)
    plan_id = models.IntegerField(null=True, blank=True)   # → tst_plan.id；判定密钥为 NULL
    key_name = models.TextField(null=True, blank=True)     # 密钥名（方案密钥「默认」/ 判定密钥「AI判定（DeepSeek）」）
    api_key = models.TextField()                           # 明文密钥
    is_active = models.IntegerField(default=1)             # 是否启用
    created_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        managed = False
        db_table = "tst_api_key"
        ordering = ["id"]

    def __str__(self):
        return self.key_name or ""


# ================================================================== 测试记录（第二次迭代起接入业务）
class TstRec(models.Model):
    """测试记录（新库的 tst_rec）。

    第二次迭代起接入业务：「提示词测试」页点「保存结果」写一条，
    记录本次调试针对哪个方案、共几题、准确率 / 错题数 / 人工矫正数各是多少；
    逐题明细落在 TstRecDtl（tst_rec_dtl）。

    字段口径按新库原样映射、不做改动：
        plan_id     → 本次调试选中的提示词方案（tst_plan.id）
        api_key_id  → 该方案用的那把密钥（tst_api_key.id，方案的「默认」密钥）
        q_cnt       问题总数        acc_rate  准确率（百分比数值）
        err_cnt     AI 判错数       fix_cnt   人工矫正数
        test_time   保存时间
    """

    id = models.AutoField(primary_key=True)
    plan_id = models.IntegerField()                        # → tst_plan.id
    api_key_id = models.IntegerField(null=True, blank=True)  # → tst_api_key.id
    q_cnt = models.IntegerField(default=0)                 # 问题总数
    acc_rate = models.DecimalField(max_digits=5, decimal_places=2,
                                   null=True, blank=True)  # 准确率
    err_cnt = models.IntegerField(default=0)               # 错误数量
    fix_cnt = models.IntegerField(default=0)               # 修正数量
    test_time = models.DateTimeField(null=True, blank=True)

    class Meta:
        managed = False
        db_table = "tst_rec"
        ordering = ["id"]

    def __str__(self):
        return "rec#%s" % self.id


class TstRecDtl(models.Model):
    """测试记录明细（新库的 tst_rec_dtl）—— 一条 = 一道题的测试结果。

    qst        测试问题
    std_ans    标准答案
    mdl_out    模型输出（页面上「测试反馈答案」列的内容）
    ai_res     AI 判定结果（right / wrong / 空 = 未判定）
    is_man_fix 是否被人工改判过（1 = 人工结论与 AI 结论不一致）
    """

    id = models.AutoField(primary_key=True)
    rec_id = models.IntegerField()                         # → tst_rec.id
    qst = models.TextField(null=True, blank=True)          # 测试问题
    std_ans = models.TextField(null=True, blank=True)      # 标准答案
    mdl_out = models.TextField(null=True, blank=True)      # 模型输出
    ai_res = models.TextField(null=True, blank=True)       # AI 判定（对 / 错）
    is_man_fix = models.IntegerField(default=0)            # 是否人工修正

    class Meta:
        managed = False
        db_table = "tst_rec_dtl"
        ordering = ["id"]

    def __str__(self):
        return "dtl#%s" % self.id


# ================================================================== 已连接数据库存档（第四次迭代）
class TstDbConn(models.Model):
    """已连接数据库的存档（新库的 tst_db_conn，第四次迭代新增）。

    一条记录 = 「数据库表结构提取」页上**已经连好的一个库**。
    存的是「连得上哪儿」+「这个库长什么样」，让用户下次进来左栏还是那些库，
    不用重新导入 / 重连。

    字段口径（与前端 sessionStorage 里 pd_db_link_meta 的形状对齐）：
        conn_name     库名 / 文件名（前端派生出来的展示名）
        conn_kind     数据库类型（SQLite / MySQL / PostgreSQL / SQL Server / Oracle / Db2 for LUW）
        conn_mode     host（主机端口）/ url（连接串）/ file（本机文件）
        host, port, dbname, user_name   连接信息
        file_path      SQLite 用：服务端落盘副本的**绝对路径**（data/uploads/ 下那份）
        schema_json    表结构快照 {"tables":[…],"relations":[…]}
        checked_json   上次勾选了哪些表 ["表名",…]
        saved_at       保存时间

    ⚠️ **本表没有、也不该有密码列。**
       前端 pdRememberConn 从设计上就不登记密码（DBeaver 侧用它自己的凭据），
       所以这里能存的只有「连得上哪儿」，认证相关的一个字都没有。
       这条与本项目「密码不过我们的手」是同一道地基 —— store.save_db_conn
       还会再做一次显式丢弃，双保险。
    """

    id = models.AutoField(primary_key=True)
    conn_name = models.TextField()                       # 库名 / 文件名
    conn_kind = models.TextField()                       # 数据库类型
    conn_mode = models.TextField(null=True, blank=True)  # host / url / file
    host = models.TextField(null=True, blank=True)
    port = models.TextField(null=True, blank=True)
    dbname = models.TextField(null=True, blank=True)
    user_name = models.TextField(null=True, blank=True)
    file_path = models.TextField(null=True, blank=True)
    schema_json = models.TextField(null=True, blank=True)   # 表结构快照
    checked_json = models.TextField(null=True, blank=True)  # 勾选态
    saved_at = models.TextField(null=True, blank=True)

    class Meta:
        managed = False
        db_table = "tst_db_conn"
        ordering = ["id"]

    def __str__(self):
        return "%s (%s)" % (self.conn_name or "", self.conn_kind or "")
