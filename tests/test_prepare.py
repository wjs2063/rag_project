"""Small representative archives exercise the disk join and spatial normalization."""

import json
import sqlite3
from pathlib import Path
from zipfile import ZipFile

import shapefile
from openpyxl import Workbook
from pyproj import Transformer

from poi_service.prepare import inspect_zip, prepare, sample_staging


def _archive(paths: list[Path], target: Path) -> None:
    with ZipFile(target, "w") as output:
        for path in paths:
            output.write(path, path.name)


def test_prepare_joins_by_ids_and_filters_categories(tmp_path: Path) -> None:
    catalog = tmp_path / "categories.csv"
    catalog.write_text("id,name,parent_id,allowed\nA,음식,,true\nB,구조물,,false\n", encoding="utf-8")
    transformer = Transformer.from_crs("EPSG:4326", "EPSG:5179", always_xy=True)
    shp = tmp_path / "TN_POI"
    writer = shapefile.Writer(str(shp), shapeType=shapefile.POINT, encoding="utf-8")
    writer.field("NF_ID", "C", size=20)
    writer.field("POIID", "C", size=20)
    writer.field("CLSS", "C", size=10)
    writer.field("NAME", "C", size=50)
    for nf_id, poiid, category, name, lon in [
        ("NF2", "P2", "B", "도로", 127.01),
        ("NF1", "P1", "A", "식당", 127.00),
    ]:
        writer.point(*transformer.transform(lon, 37.5))
        writer.record(nf_id, poiid, category, name)
    writer.close()
    _archive([shp.with_suffix(ext) for ext in (".shp", ".shx", ".dbf")], tmp_path / "poi.zip")
    poi_member = next(item for item in inspect_zip(tmp_path / "poi.zip", headers=True)
                      if item["name"] == "TN_POI.dbf")
    assert poi_member["dbf_rows"] == 2
    assert "NF_ID" in poi_member["fields"]

    address = tmp_path / "addr"
    writer = shapefile.Writer(str(address), shapeType=shapefile.NULL, encoding="utf-8")
    writer.field("NF_ID", "C", size=20)
    writer.field("ADDR", "C", size=80)
    for nf_id, text in [("NF1", "서울 주소"), ("NF2", "다른 주소")]:
        writer.null()
        writer.record(nf_id, text)
    writer.close()
    _archive([address.with_suffix(".dbf")], tmp_path / "addr.zip")

    alias = tmp_path / "aliases"
    writer = shapefile.Writer(str(alias), shapeType=shapefile.NULL, encoding="utf-8")
    writer.field("POIID", "C", size=20)
    writer.field("ALIAS", "C", size=50)
    writer.null()
    writer.record("P1", "별칭 식당")
    writer.close()
    _archive([alias.with_suffix(".dbf")], tmp_path / "aliases.zip")

    workbook = Workbook()
    sheet = workbook.active
    sheet.append(["NF_ID"])
    sheet.append(["NF1"])
    sheet.append(["NF2"])
    workbook.save(tmp_path / "check.xlsx")
    _archive([tmp_path / "check.xlsx"], tmp_path / "xlsx.zip")

    manifest = {
        "catalog_csv": "categories.csv",
        "catalog_version": "test-v1",
        "source_version": "test",
        "global_auxiliary": [
            {
                "name": "global:aliases",
                "zip": "aliases.zip",
                "member": "aliases.dbf",
                "key_field": "POIID",
                "join_on": "POIID",
                "encoding": "utf-8",
                "fields": {"alias": "ALIAS"},
            }
        ],
        "regions": [
            {
                "name": "seoul",
                "zip": "poi.zip",
                "shp_member": "TN_POI.shp",
                "shx_member": "TN_POI.shx",
                "dbf_member": "TN_POI.dbf",
                "encoding": "utf-8",
                "fields": {"nf_id": "NF_ID", "poiid": "POIID", "category_id": "CLSS", "name": "NAME"},
                "auxiliary": [
                    {
                        "name": "address",
                        "zip": "addr.zip",
                        "member": "addr.dbf",
                        "key_field": "NF_ID",
                        "fields": {"address": ["MISSING", "ADDR"]},
                        "encoding": "utf-8",
                    }
                ],
                "xlsx_check": {"zip": "xlsx.zip", "member": "check.xlsx"},
            }
        ],
    }
    source = tmp_path / "manifest.json"
    source.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    target = tmp_path / "staging.sqlite3"
    report = prepare(source, target)
    assert report["selected_total"] == 1
    assert report["regions"]["seoul"]["source_rows"] == 2
    assert report["regions"]["seoul"]["xlsx_rows"] == 2
    assert report["regions"]["seoul"]["excluded_category"] == 1
    with sqlite3.connect(target) as connection:
        row = json.loads(connection.execute("SELECT payload FROM pois").fetchone()[0])
    assert row["nf_id"] == "NF1"
    assert row["address"] == "서울 주소"
    assert row["aliases"] == "별칭 식당"
    assert abs(row["longitude"] - 127) < 1e-5


def test_xlsx_aliases_join_by_poiid_and_sample_is_repeatable(tmp_path: Path) -> None:
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(["POIID", "ALIAS"])
    sheet.append([101, "서울 별칭"])
    workbook.save(tmp_path / "aliases.xlsx")
    _archive([tmp_path / "aliases.xlsx"], tmp_path / "aliases.zip")
    alias_member = inspect_zip(tmp_path / "aliases.zip", headers=True)[0]
    assert alias_member["sheets"]["Sheet"] == ["POIID", "ALIAS"]
    from poi_service.prepare import _load_aux, _lookup, _schema

    db = tmp_path / "source.sqlite3"
    with sqlite3.connect(db) as connection:
        _schema(connection)
        counts = _load_aux(connection, tmp_path, [{
            "name": "alias", "zip": "aliases.zip", "member": "aliases.xlsx",
            "key_field": "POIID", "fields": {"alias": ["ALIAS", "MISSING"]},
        }], "seoul")
        assert counts["aux_alias_rows"] == 1
        assert _lookup(connection, "alias", "101") == [{"alias": "서울 별칭"}]
        for region in ("seoul", "gyeonggi"):
            for index in range(3):
                nf_id = f"{region}-{index}"
                connection.execute("INSERT INTO pois VALUES (?,?)", (
                    nf_id, json.dumps({"source_region": region, "name": nf_id})
                ))

    first = tmp_path / "sample1.sqlite3"
    second = tmp_path / "sample2.sqlite3"
    assert sample_staging(db, first, ["seoul", "gyeonggi"], 2)["selected_total"] == 4
    sample_staging(db, second, ["seoul", "gyeonggi"], 2)
    def ids(path: Path) -> list[str]:
        with sqlite3.connect(path) as connection:
            return [row[0] for row in connection.execute("SELECT nf_id FROM pois ORDER BY nf_id")]
    assert ids(first) == ids(second)
