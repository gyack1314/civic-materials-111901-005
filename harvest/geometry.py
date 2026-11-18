"""几何与坐标处理：轴序归一化、矩形分割、覆盖与缺口。

契约约定 bbox 输入始终按「经纬度」（minx,miny,maxx,maxy，x=经度 lon，y=纬度 lat）；
响应通过 axis 声明输出坐标顺序，crs 声明坐标系。本模块把所有落库几何统一为
GeoJSON 经纬度（lon,lat）顺序，CRS 随响应原文记录，不做投影转换。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from shapely.geometry import GeometryCollection, MultiPolygon, Polygon, box, shape
from shapely.ops import unary_union

# 相对根矩形的最小边长比例：低于该跨度不再分割，避免无限细分。
MIN_SPAN_RATIO = 1e-7
# 单次运行的任务预算：真实来源下远不会触及；恶意/异常超限时转为缺口而非无限循环。
DEFAULT_TASK_BUDGET = 2000
MAX_DEPTH = 32
# bbox 落库时保留的小数位，保证同一矩形在续跑时生成相同任务键。
BBOX_PRECISION = 10


def round_bbox(minx: float, miny: float, maxx: float, maxy: float) -> tuple[float, float, float, float]:
    """统一矩形数值精度，作为任务去重与续跑的稳定键。"""
    return tuple(round(float(v), BBOX_PRECISION) for v in (minx, miny, maxx, maxy))


def bbox_key(bbox: tuple[float, float, float, float]) -> str:
    return ",".join(str(v) for v in round_bbox(*bbox))


def bbox_polygon(bbox: tuple[float, float, float, float]) -> Polygon:
    return box(*round_bbox(*bbox))


def parse_boundary(geojson: dict[str, Any]) -> tuple[Polygon | MultiPolygon, int | None]:
    """解析市域 Feature，返回归一化多边形与其声明的 revision。"""
    geom = shape(geojson["geometry"])
    if isinstance(geom, GeometryCollection):
        geom = unary_union([g for g in geom.geoms if isinstance(g, (Polygon, MultiPolygon))])
    if not isinstance(geom, (Polygon, MultiPolygon)):
        raise ValueError(f"市域边界必须是多边形，实际为 {geom.geom_type}")
    revision = (geojson.get("properties") or {}).get("revision")
    return geom, revision


def normalize_feature(feature: dict[str, Any], axis: str) -> dict[str, Any]:
    """按响应 axis 声明把 Feature 几何归一化为 lon,lat 顺序。

    axis='lon,lat' 时坐标即经纬度，原样保留；
    axis='lat,lon'（api=yx）时每个坐标对需要交换。
    输入 Feature 不会被修改，返回深拷贝。
    """
    import copy

    feature = copy.deepcopy(feature)
    if axis == "lon,lat":
        return feature
    if axis != "lat,lon":
        raise ValueError(f"未知 axis 声明：{axis!r}，拒绝按猜测解释坐标")

    def swap(coords):
        if isinstance(coords[0], (int, float)):
            return [coords[1], coords[0]]
        return [swap(c) for c in coords]

    feature["geometry"]["coordinates"] = swap(feature["geometry"]["coordinates"])
    return feature


@dataclass(frozen=True)
class Tile:
    """一次查询所声明的空间范围与类别通道。

    category 为 None 表示全量通道；具体类别表示该次查询只证明该类别。
    """

    bbox: tuple[float, float, float, float]
    category: str | None

    @property
    def key(self) -> str:
        return f"{bbox_key(self.bbox)}|{self.category or '*'}"


def split_bbox(bbox: tuple[float, float, float, float]) -> list[tuple[float, float, float, float]]:
    """把矩形沿较长边平分（二等分），返回仍有正向面积的子矩形。

    采用二等分而非四等分：同一位置密集设施在一个维度上即可被分离，
    子任务数量增长更温和；边长过小（低于 MIN_SPAN_RATIO 相对根跨度）时返回空列表，
    由调用方登记为缺口，杜绝无限分割。
    """
    minx, miny, maxx, maxy = round_bbox(*bbox)
    width, height = maxx - minx, maxy - miny
    if width >= height and width > 0:
        mid = round((minx + maxx) / 2, BBOX_PRECISION)
        candidates = [
            (minx, miny, mid, maxy),
            (mid, miny, maxx, maxy),
        ]
    elif height > 0:
        mid = round((miny + maxy) / 2, BBOX_PRECISION)
        candidates = [
            (minx, miny, maxx, mid),
            (minx, mid, maxx, maxy),
        ]
    else:
        return []
    result = []
    for child in candidates:
        cx0, cy0, cx1, cy1 = child
        if cx1 - cx0 > 0 and cy1 - cy0 > 0 and child != (minx, miny, maxx, maxy):
            result.append(child)
    return result


def min_span(root_bbox: tuple[float, float, float, float]) -> float:
    """根矩形决定的最小可分割跨度。"""
    minx, miny, maxx, maxy = root_bbox
    return max(maxx - minx, maxy - miny) * MIN_SPAN_RATIO


def intersects_boundary(
    bbox: tuple[float, float, float, float],
    boundary: Polygon | MultiPolygon,
) -> bool:
    """矩形是否与市域有真实相交（含边界接触，契约规定相交含接触）。"""
    return bbox_polygon(bbox).intersects(boundary)


def clip_to_boundary(
    bboxes: list[tuple[float, float, float, float]],
    boundary: Polygon | MultiPolygon,
) -> Polygon | MultiPolygon | None:
    """把查询矩形的并集裁剪到市域多边形：覆盖范围只算市域内的部分。"""
    if not bboxes:
        return None
    union = unary_union([bbox_polygon(b) for b in bboxes])
    covered = union.intersection(boundary)
    if covered.is_empty:
        return None
    if isinstance(covered, GeometryCollection):
        parts = [g for g in covered.geoms if isinstance(g, (Polygon, MultiPolygon)) and not g.is_empty]
        covered = unary_union(parts) if parts else None
    return covered


def boundary_area(boundary: Polygon | MultiPolygon) -> float:
    return float(boundary.area)


def geometry_intersects_boundary(
    geometry: dict[str, Any], boundary: Polygon | MultiPolygon
) -> bool:
    """设施/地块真实几何是否与市域相交（含接触）——不是中心点判断，也不是外接矩形。"""
    return shape(geometry).intersects(boundary)


def intersection_footprint(
    geometry: dict[str, Any], boundary: Polygon | MultiPolygon
) -> dict[str, Any] | None:
    """地块几何与市域的真实相交部分，作为覆盖证明几何落库。"""
    inter = shape(geometry).intersection(boundary)
    if inter.is_empty:
        return None
    return inter.__geo_interface__


def to_multipolygon(geom: Polygon | MultiPolygon | None) -> MultiPolygon | None:
    if geom is None:
        return None
    if isinstance(geom, MultiPolygon):
        return geom
    return MultiPolygon([geom])
