# 高德地点与餐饮闭环

## 1. 本阶段目标

本阶段把“地点”和“餐饮”从行程文案中的弱结构描述，升级为可查询、可裁剪、可持久化、可复验的确定性数据链路。

核心原则：

- 事实数据优先由高德工具直接获取，不增加不必要的 LLM 调用。
- 外部查询必须有数量上限，防止多日行程产生无界 HTTP 请求。
- 高德原始 JSON 只在 Provider 层出现，Agent 和业务层只使用稳定的 Pydantic 模型。
- 餐厅查询必须在酒店、景点、路线和日程基本稳定后执行，避免锚点变化导致重复查询。
- 真实餐厅不可用时允许确定性回退，但必须在返回结果中标明数据来源。

## 2. 高德 Web 服务接口

项目当前只调用高德 Web 服务 API，不依赖高德应用地图 SDK、JavaScript API 或移动端 SDK。

本阶段使用的地点接口：

| 能力 | 接口 | 用途 |
| --- | --- | --- |
| 关键词地点搜索 | `/v5/place/text` | 通用 POI、景点、酒店和地点解析 |
| 周边地点搜索 | `/v5/place/around` | 餐厅和动态候选池搜索 |
| POI 详情 | `/v5/place/detail` | 按 POI ID 获取稳定地点详情 |
| 地理编码 | `/v3/geocode/geo` | POI 搜索无结果时把地址解析为坐标 |

地点搜索统一请求 `business` 扩展字段，并在标准化层兼容旧数据中的 `biz_ext` 字段。

## 3. 标准化输出模型

`app/providers/amap/models.py` 新增以下稳定模型：

- `PoiCandidate` / `PoiSearchResult`
- `RestaurantCandidate` / `RestaurantSearchAnchor` / `RestaurantSearchResult`
- `PoiDetailResult`
- `LocationResolutionResult`

标准化层位于 `app/providers/amap/normalizers.py`，负责：

- 解析字符串、数组和对象形式的坐标。
- 过滤缺少名称或有效坐标的 POI。
- 地址缺失时依次回退到行政区和“地址待确认”。
- 解析评分、人均消费、营业时间、电话、类型编码和距离。
- 按 POI ID 或“名称 + 地址”去重。
- 通用 POI 按评分和原始顺序稳定排序。
- 餐厅按距离优先、评分次之、名称稳定排序。
- 根据配置裁剪候选数量。

## 4. ToolRegistry 新工具

`app/tools/trip_registry.py` 新增四个零 LLM 成本工具：

| 工具 | 作用 |
| --- | --- |
| `search_pois` | 文本或周边通用地点搜索 |
| `search_restaurants` | 围绕多个餐次锚点批量搜索真实餐厅 |
| `get_poi_detail` | 按 POI ID 查询详情 |
| `resolve_location` | POI 搜索优先、地理编码回退的地点解析 |

所有工具均经过输入 Schema、输出 Schema、错误分类和 ToolRegistry 白名单约束。注入的旧 Provider 没有实现新能力时，工具会返回空的标准化结果，不会因为 `AttributeError` 中断旧测试或旧部署。

## 5. 餐饮锚点规则

餐饮锚点由 `app/plan_content/dining.py` 根据最终行程确定性生成：

- 早餐：优先使用当天出发酒店；没有酒店时使用当天第一个或中间景点。
- 午餐：优先使用当天中间景点；没有景点时使用酒店。
- 晚餐：优先使用当天返回酒店；没有酒店时使用最后一个景点。
- 地点没有坐标或名称时跳过对应锚点。

裁剪优先级：

1. 每天午餐
2. 首日早餐
3. 每天晚餐
4. 其余早餐

该顺序优先保障景点附近午餐，同时尽量覆盖首日出发和每日返程场景。

## 6. 有界查询与结果复用

餐厅搜索具有三层边界：

- 单次行程最多查询的锚点数。
- 每个锚点最多保留的候选数。
- 周边搜索半径上限。

如果早餐和晚餐使用同一酒店坐标，Provider 只执行一次周边 HTTP 查询，再把同一批标准化候选分别绑定到两个餐次锚点。这样可以降低延迟、调用成本和限流风险。

## 7. Agent 确定性循环接入

AgentState 版本已升级，并新增：

- `AgentAction.SEARCH_RESTAURANTS`
- `restaurants`
- `restaurant_plan_fingerprint`

餐厅查询位于以下阶段之后：

- 路线质量评估和顺序优化
- 单段通勤评估和过远景点替换
- 日程优化和约束优化
- 最低景点保障与候选回填

随后才执行内容重建、最终约束评估和校验。这样可以确保餐厅候选对应最终的酒店、景点和地点时间轴。

餐饮搜索锚点使用稳定 SHA-256 指纹。酒店、景点或坐标发生变化时，旧餐厅结果会失效并重新搜索；锚点未变化时复用已有结果，避免重复调用高德。

## 8. 内容重建与回退

`TripPlanConsistencyRebuilder` 在重建每天三餐时：

1. 按 `day_index + meal_type` 查找候选。
2. 距离近优先，评分作为次级排序。
3. 同一天不重复选择同一家餐厅。
4. 有高德人均消费时使用真实值重建餐饮预算。
5. 没有消费字段时使用确定性默认餐费。
6. 有真实候选时设置 `source="amap"`。
7. 无真实候选时生成附近餐饮建议并设置 `source="fallback"`。

`Meal` 兼容新增字段：

- `poi_id`
- `rating`
- `telephone`
- `category`
- `opening_hours`
- `source`

这些字段都有默认值，因此旧 API 响应和旧 SQLite 会话仍可恢复。

## 9. 配置

`.env.example` 新增：

```dotenv
AMAP_MAX_POI_CANDIDATES=10
AMAP_MAX_RESTAURANT_CANDIDATES_PER_ANCHOR=4
AMAP_MAX_RESTAURANT_SEARCH_ANCHORS=8
AMAP_RESTAURANT_SEARCH_RADIUS_METERS=2500
```

真实 `.env` 需要由部署环境自行维护，本阶段不自动修改真实密钥或运行参数。

## 10. 当前边界

本阶段已经完成真实餐厅 POI 闭环，但仍有以下边界：

- 营业时间来自查询快照，尚未结合具体到店时间做强约束校验。
- 高德返回的人均消费可能缺失，预算仍需允许默认值回退。
- 尚未接入排队、订座、临时停业、套餐价格和用户饮食禁忌数据源。
- 尚未建立餐饮查询 SQLite 缓存；当前只在单次 Provider 调用内复用相同坐标结果。
- `resolve_location` 已提供工具能力，但还没有覆盖所有用户自由文本入口。

## 11. 测试范围

`tests/test_amap_poi_dining.py` 覆盖：

- POI v5 请求参数和接口路径。
- `business` 字段解析与地址回退。
- 餐厅距离排序、锚点绑定和候选裁剪。
- 相同坐标 HTTP 结果复用。
- POI 详情、精确地点解析和 geocode 回退。
- 四个新工具及旧 Provider 兼容。
- 真实餐厅内容重建、fallback 和单日去重。
- 餐饮锚点指纹变化。
- 旧 AgentState 缺少餐饮字段时的恢复兼容。


## 11. 餐饮营业时间约束与 SQLite 缓存

本阶段继续补齐餐饮闭环：

- `restaurant_hours.py` 支持单区间、多区间、全天营业和跨午夜营业时间。
- 早餐默认使用 `08:00-08:45`；午餐优先复用时间轴中的 meal；晚餐从 `18:00` 和当天时间轴结束时间中的较晚值开始。
- 餐厅选择顺序为：营业时间确认覆盖、营业时间未知、确定性回退；明确关闭的候选不会被选择。
- `Meal` 新增 `planned_start_time`、`planned_end_time` 和 `opening_status`，并保留默认值兼容旧会话。
- `ConstraintEvaluator` 会输出 `meal.outside_opening_hours`，但营业时间缺失不会被误判为错误。
- `RestaurantSearchSnapshot` 只保存稳定 POI 字段，不保存 `day_index`、`meal_type` 和 `anchor_id`。
- `SQLiteRestaurantCache` 使用独立 `restaurant_cache` 表，过期读取会自动删除，也支持批量 `purge_expired()`。
- 缓存键包含城市、关键词、餐饮类型、中心坐标、半径和候选上限，防止不同查询互相污染。
- 缓存异常只降级为实时高德查询，不会把成功的餐饮工具调用变成 Agent 失败。

新增配置：

```dotenv
AMAP_RESTAURANT_CACHE_ENABLED=true
AMAP_RESTAURANT_CACHE_TTL_SECONDS=21600
```

默认 TTL 为 6 小时。真实 `.env` 是否覆盖这两个值由部署环境决定，代码不会自动修改真实密钥或环境文件。
