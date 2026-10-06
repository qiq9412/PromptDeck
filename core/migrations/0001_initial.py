# -*- coding: utf-8 -*-
"""初始迁移 —— 只登记模型状态，不建任何表。

（Django 6.0.8 生成，本次迭代换库后由 makemigrations 重新产出。）

下面 7 个 CreateModel 的 options 里全部带 `managed: False`：
它们的表（srv / mdl / tst_plan / tst_plan_prompt / tst_api_key /
tst_rec / tst_rec_dtl）结构由 promptdeck/migrate_to_twe.py 一次性建好并搬完数据，
Django 只负责读写、不建表不改表，所以 migrate 跑下来只会落下
django_migrations 这一张记账表。

上一版那个「建 tp 表」的迁移已随换库作废 —— 提示词方案现在拆存
tst_plan + tst_plan_prompt + tst_api_key 三张表。
"""

from django.db import migrations, models


class Migration(migrations.Migration):

    initial = True

    dependencies = [
    ]

    operations = [
        migrations.CreateModel(
            name='Mdl',
            fields=[
                ('id', models.AutoField(primary_key=True, serialize=False)),
                ('srv_id', models.IntegerField()),
                ('mdl_name', models.TextField()),
                ('mdl_type', models.TextField(null=True)),
                ('in_price', models.TextField(null=True)),
                ('out_price', models.TextField(null=True)),
                ('unit', models.TextField(null=True)),
                ('url', models.TextField(null=True)),
            ],
            options={
                'db_table': 'mdl',
                'ordering': ['id'],
                'managed': False,
            },
        ),
        migrations.CreateModel(
            name='Srv',
            fields=[
                ('id', models.AutoField(primary_key=True, serialize=False)),
                ('plat_name', models.TextField()),
                ('base_url', models.TextField(null=True)),
                ('comp_name', models.TextField(null=True)),
                ('home_url', models.TextField(null=True)),
            ],
            options={
                'db_table': 'srv',
                'ordering': ['id'],
                'managed': False,
            },
        ),
        migrations.CreateModel(
            name='TstApiKey',
            fields=[
                ('id', models.AutoField(primary_key=True, serialize=False)),
                ('plan_id', models.IntegerField()),
                ('key_name', models.TextField(blank=True, null=True)),
                ('api_key', models.TextField()),
                ('is_active', models.IntegerField(default=1)),
                ('created_at', models.DateTimeField(blank=True, null=True)),
                ('updated_at', models.DateTimeField(blank=True, null=True)),
            ],
            options={
                'db_table': 'tst_api_key',
                'ordering': ['id'],
                'managed': False,
            },
        ),
        migrations.CreateModel(
            name='TstPlan',
            fields=[
                ('id', models.AutoField(primary_key=True, serialize=False)),
                ('plan_name', models.TextField(blank=True, null=True)),
                ('base_url', models.TextField(blank=True, null=True)),
                ('srv_id', models.IntegerField()),
                ('mdl_id', models.IntegerField()),
            ],
            options={
                'db_table': 'tst_plan',
                'ordering': ['id'],
                'managed': False,
            },
        ),
        migrations.CreateModel(
            name='TstPlanPrompt',
            fields=[
                ('id', models.AutoField(primary_key=True, serialize=False)),
                ('plan_id', models.IntegerField()),
                ('seq_no', models.IntegerField()),
                ('prompt_role', models.TextField()),
                ('prompt_content', models.TextField()),
            ],
            options={
                'db_table': 'tst_plan_prompt',
                'ordering': ['plan_id', 'seq_no'],
                'managed': False,
            },
        ),
        migrations.CreateModel(
            name='TstRec',
            fields=[
                ('id', models.AutoField(primary_key=True, serialize=False)),
                ('plan_id', models.IntegerField()),
                ('api_key_id', models.IntegerField(blank=True, null=True)),
                ('q_cnt', models.IntegerField(default=0)),
                ('acc_rate', models.DecimalField(blank=True, decimal_places=2, max_digits=5, null=True)),
                ('err_cnt', models.IntegerField(default=0)),
                ('fix_cnt', models.IntegerField(default=0)),
                ('test_time', models.DateTimeField(blank=True, null=True)),
            ],
            options={
                'db_table': 'tst_rec',
                'ordering': ['id'],
                'managed': False,
            },
        ),
        migrations.CreateModel(
            name='TstRecDtl',
            fields=[
                ('id', models.AutoField(primary_key=True, serialize=False)),
                ('rec_id', models.IntegerField()),
                ('qst', models.TextField(blank=True, null=True)),
                ('std_ans', models.TextField(blank=True, null=True)),
                ('mdl_out', models.TextField(blank=True, null=True)),
                ('ai_res', models.TextField(blank=True, null=True)),
                ('is_man_fix', models.IntegerField(default=0)),
            ],
            options={
                'db_table': 'tst_rec_dtl',
                'ordering': ['id'],
                'managed': False,
            },
        ),
    ]
