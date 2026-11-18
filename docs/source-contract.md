# 本地来源契约

GET /boundary 返回市域 GeoJSON，revision 随来源阶段变化。GET /catalog?bbox=minx,miny,maxx,maxy&api=xy&category=solar 按矩形与地块几何相交返回最多两项。truncated=true 明确有更多记录，false 表示该筛选范围未截断，空列表也有同等范围意义。category 可省略；categories 为来源声明的完整分类集合，分类互斥。api=xy 返回经纬度，api=yx 返回纬经度，bbox 输入始终按经纬度。响应 axis 与 crs 声明输出解释。
Feature 的 properties 含 parcel、facility、category、permit；地块主键 parcel，设施主键 facility，一个设施可有多个地块。同址密集设施可用分类过滤，某分类仍截断且无法继续定位时来源没有保证能遍历完毕。许可为独立 PDF 原件，页内文字声明 facility，不以 URL 就认定归属。
phase=1 改变市域且令 F-A 许可暂时声明其他设施，phase=2 恢复许可身份但保持新版边界。几何相交含边界接触，市域为带缺口的多边形，原始范围与更版范围见实际 GeoJSON。

演练来源由 Python 标准库 HTTP 服务启动，默认端口 8765。GET /health 可查询来源时间与阶段；POST /__admin/advance 接收 seconds、phase 推进本地情形；GET /__admin/log 回查实际请求。这些控制只供操作员调试来源，不能替代产品从正常入口取得资料。来源进程重启会清空其演练状态；采集端自己的保存目录由产品独立管理。
