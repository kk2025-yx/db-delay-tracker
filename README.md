# DB 火车延误数据采集与分析 (v2)

## 跟之前版本的区别

根据你在网页上手动测试 `fchg` 实际拿到的数据结构做了调整：

- 新增了 `delay_reason_code` 字段（延误原因分类码，来自 `<m t="d" c="...">`）
- 新增了 `notices` 表，专门存 `<m t="h">` 这类"公告"消息（Bauarbeiten施工、Störung故障、Information提示），
  这些消息带 `from`/`to` 有效期，能帮你判断某天的延误是不是因为长期施工/故障（而不是偶发问题）
- `stops` 表里 `planned_time` 允许为空——因为 `fchg` 里有些记录只有 `ct`（变更后时间）没有 `pt`（计划时间），
  这种情况下延误分钟数算不出来，先存 NULL，以后需要的话可以另外调 `plan` 接口把计划时间补上
- 每次抓取都会存成新的一行（哪怕同一趟车同一站），这是特意设计的：同一趟车的预计到达时间在运行过程中会
  被多次更新（你在 `fchg` 数据里能看到同一个 `stop_id` 下有好几条 `<m>` 时间戳），持续记录能让你看到
  "这趟车的延误是怎么一步步变化/恶化的"，而不只是最终结果

## 文件说明

- `stations.py` — 站点名和 EVA number 的映射表，Memmingen 已经帮你填好（8000249），
  其余 5 个（Buchloe、Kaufering、Augsburg Hbf、Messe Augsburg、München Hbf）你去查完后补上
- `collect.py` — 采集脚本，调用 `fchg` 接口
- `requirements.txt` — Python 依赖
- `.github/workflows/collect.yml` — 定时任务配置，每 15 分钟自动跑一次

## 使用步骤

### 1. 本地测试

```bash
cd db-delay-tracker-v2
pip install -r requirements.txt
```

把 `stations.py` 里剩下的 `REPLACE_ME` 换成实际 EVA number。

```bash
export DB_CLIENT_ID="你的client_id"
export DB_API_KEY="你的client_secret"
python collect.py
```

正常的话会看到类似：
```
[Memmingen / 8000249] 抓到 45 条停靠记录（新增 45），8 条公告（新增 3）
完成。本次共新增 XX 条停靠记录，XX 条公告。
```

同目录会生成 `data.db`。可以用 `DB Browser for SQLite`（免费图形化工具）打开看看数据长什么样。

### 2. 放到 GitHub 自动定时跑

同 v1，参考之前的说明：建仓库 → 配置 `DB_CLIENT_ID` / `DB_API_KEY` 两个 Secret → 推送代码 →
在 Actions 标签页手动跑一次测试 → 确认没问题后让它按 cron 自动跑。

### 3. 之后怎么用数据分析延误规律

`data.db` 攒够一段时间后，几个建议的分析方向：

```sql
-- 某个车次的历次延误分钟数
SELECT fetched_at, planned_time, changed_time, delay_minutes, delay_reason_code
FROM stops
WHERE train_number = '78940' AND kind = 'departure'
ORDER BY fetched_at;

-- 按延误原因码统计频率
SELECT delay_reason_code, COUNT(*) as cnt
FROM stops
WHERE delay_minutes IS NOT NULL AND delay_minutes > 0
GROUP BY delay_reason_code
ORDER BY cnt DESC;

-- 看看某天是否处于施工/故障公告的有效期内
SELECT category, valid_from, valid_to, COUNT(*) as mentioned_in_n_stops
FROM notices
GROUP BY category, valid_from, valid_to
ORDER BY valid_from;
```

延误原因码（`c` 属性）目前没有官方公开的完整对照表，可以先把出现频率最高的几个码记录下来，
之后通过实际观察（比如某天遇到这个原因码时正好新闻里有相关报道）反推大概含义，这也是数据分析里
"探索性分析"的一部分。
