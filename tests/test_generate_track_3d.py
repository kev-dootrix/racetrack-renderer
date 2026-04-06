from __future__ import annotations

import contextlib
import json
import math
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import generate_track_3d as gt3d


def make_geometry(title: str = "Brands Hatch") -> gt3d.GeometryResult:
    origin = gt3d.LatLonPoint(lat=51.3579, lon=0.2609)
    local_points = [
        gt3d.LocalPoint(0.0, 0.0),
        gt3d.LocalPoint(120.0, 0.0),
        gt3d.LocalPoint(150.0, 90.0),
        gt3d.LocalPoint(0.0, 140.0),
        gt3d.LocalPoint(0.0, 0.0),
    ]
    geographic_points = [
        gt3d.LatLonPoint(
            lat=origin.lat + point.z / 111132.0,
            lon=origin.lon + point.x / (111320.0 * math.cos(math.radians(origin.lat))),
        )
        for point in local_points
    ]
    distances_m = gt3d.cumulative_dist(local_points)
    return gt3d.GeometryResult(
        title=title,
        source_label="OpenStreetMap raceway geometry",
        source_note="OSM highway=raceway geometry via Nominatim + Overpass",
        source_urls={
            "nominatim": "https://nominatim.openstreetmap.org",
            "overpass": "https://overpass-api.de",
        },
        geographic_points=geographic_points,
        local_points=local_points,
        distances_m=distances_m,
        projection_origin=origin,
        total_length_m=distances_m[-1],
        metadata={"geometry_source": "osm_raceway"},
    )


class GenerateTrack3DTests(unittest.TestCase):
    def test_trusted_geometry_scales_to_configured_track_length(self) -> None:
        geometry = make_geometry()
        track_config = json.loads(Path("track_configs/brandshatch.json").read_text(encoding="utf-8"))

        with mock.patch.object(gt3d, "load_centerline_points", return_value=geometry.local_points), mock.patch.object(
            gt3d, "resolve_geometry_anchor", return_value=geometry.projection_origin
        ):
            result = gt3d.resolve_trusted_geometry_geometry_only(
                track_query="Brands Hatch",
                event_fields=["Brands Hatch", "Brands Hatch"],
                track_config=track_config,
                projection_origin=geometry.projection_origin,
            )

        self.assertIsNotNone(result)
        assert result is not None
        self.assertAlmostEqual(result.total_length_m, track_config["track_length_m"], delta=1.0)
        self.assertAlmostEqual(result.metadata["target_track_length_m"], track_config["track_length_m"], delta=0.001)
        self.assertGreater(result.metadata["geometry_length_scale_factor"], 1.0)

    def test_brands_hatch_geometry_only_writes_html_and_cached_dem_profile(self) -> None:
        geometry = make_geometry()

        with tempfile.TemporaryDirectory() as tmpdir:
            output_root = Path(tmpdir)
            with (
                open(Path(tmpdir) / "stdout.txt", "w", encoding="utf-8") as stdout,
                open(Path(tmpdir) / "stderr.txt", "w", encoding="utf-8") as stderr,
                contextlib.redirect_stdout(stdout),
                contextlib.redirect_stderr(stderr),
                mock.patch.object(gt3d, "resolve_event", return_value=None),
                mock.patch.object(gt3d, "resolve_trusted_geometry_geometry_only", return_value=geometry),
                mock.patch.object(
                    gt3d.OSMGeometryProvider,
                    "resolve",
                    side_effect=AssertionError("OSM geometry should not be used when a trusted centerline exists"),
                ),
                mock.patch.object(gt3d, "fetch_opentopodata_elevations", return_value=[12.5, 16.0, 19.0, 15.5]),
            ):
                html_path = gt3d.render_track_3d(
                    track="Brands Hatch",
                    year=None,
                    session="Q",
                    output_root_value=str(output_root),
                    track_width_m=18.0,
                    elevation_scale=1.0,
                    track_depth=18.0,
                )

            track_root = output_root / "Brands Hatch"
            profile_path = track_root / "brands-hatch_elevation_profile.json"
            metadata_path = track_root / "source_metadata.json"

            self.assertEqual(html_path.name, "brands-hatch-3d.html")
            self.assertEqual(html_path.parent.name, "Brands Hatch")
            self.assertTrue(html_path.exists())
            self.assertTrue(profile_path.exists())
            self.assertTrue(metadata_path.exists())

            html_text = html_path.read_text(encoding="utf-8")
            self.assertIn("Geometry-only • OpenTopoData DEM", html_text)

            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            self.assertIsNone(metadata["event"]["year"])
            self.assertEqual(metadata["data_files"]["elevation_profile"], profile_path.name)
            self.assertIn("OpenTopoData DEM samples cached in the track folder", metadata["sources"])

    def test_brands_hatch_geometry_only_reuses_cached_dem_profile(self) -> None:
        geometry = make_geometry()

        with tempfile.TemporaryDirectory() as tmpdir:
            output_root = Path(tmpdir)
            first_fetch = [12.5, 16.0, 19.0, 15.5]
            with (
                open(Path(tmpdir) / "stdout-1.txt", "w", encoding="utf-8") as stdout1,
                open(Path(tmpdir) / "stderr-1.txt", "w", encoding="utf-8") as stderr1,
                contextlib.redirect_stdout(stdout1),
                contextlib.redirect_stderr(stderr1),
                mock.patch.object(gt3d, "resolve_event", return_value=None),
                mock.patch.object(gt3d, "resolve_trusted_geometry_geometry_only", return_value=geometry),
                mock.patch.object(
                    gt3d.OSMGeometryProvider,
                    "resolve",
                    side_effect=AssertionError("OSM geometry should not be used when a trusted centerline exists"),
                ),
                mock.patch.object(gt3d, "fetch_opentopodata_elevations", return_value=first_fetch),
            ):
                gt3d.render_track_3d(
                    track="Brands Hatch",
                    year=None,
                    session="Q",
                    output_root_value=str(output_root),
                    track_width_m=18.0,
                    elevation_scale=1.0,
                    track_depth=18.0,
                )

            with (
                open(Path(tmpdir) / "stdout-2.txt", "w", encoding="utf-8") as stdout2,
                open(Path(tmpdir) / "stderr-2.txt", "w", encoding="utf-8") as stderr2,
                contextlib.redirect_stdout(stdout2),
                contextlib.redirect_stderr(stderr2),
                mock.patch.object(gt3d, "resolve_event", return_value=None),
                mock.patch.object(gt3d, "resolve_trusted_geometry_geometry_only", return_value=geometry),
                mock.patch.object(
                    gt3d.OSMGeometryProvider,
                    "resolve",
                    side_effect=AssertionError("OSM geometry should not be used when a trusted centerline exists"),
                ),
                mock.patch.object(
                    gt3d,
                    "fetch_opentopodata_elevations",
                    side_effect=AssertionError("cached profile should avoid a remote DEM call"),
                ),
            ):
                gt3d.render_track_3d(
                    track="Brands Hatch",
                    year=None,
                    session="Q",
                    output_root_value=str(output_root),
                    track_width_m=18.0,
                    elevation_scale=1.0,
                    track_depth=18.0,
                )

            profile_path = output_root / "Brands Hatch" / "brands-hatch_elevation_profile.json"
            profile = json.loads(profile_path.read_text(encoding="utf-8"))
            self.assertEqual(profile["sample_count"], 4)
            self.assertEqual(profile["metadata"]["provider"], "OpenTopoData")

    def test_opentopodata_provider_normalizes_and_caches_profile(self) -> None:
        geometry = make_geometry()
        provider = gt3d.OpenTopoDataElevationProvider(elevation_scale=1.0, datasets=("eudem25m", "srtm30m"))

        with tempfile.TemporaryDirectory() as tmpdir:
            track_root = Path(tmpdir)
            with mock.patch.object(
                gt3d,
                "fetch_opentopodata_elevations",
                return_value=[120.0, None, 150.0, 135.0],
            ):
                result = provider.resolve("Brands Hatch", None, geometry, track_root)

            self.assertEqual(len(result.elevations_m), 4)
            self.assertEqual(min(result.elevations_m), 0.0)
            self.assertGreater(max(result.elevations_m), 0.0)
            self.assertEqual(result.metadata["provider"], "OpenTopoData")

            cache_path = track_root / "brands-hatch_elevation_profile.json"
            self.assertTrue(cache_path.exists())

            with mock.patch.object(
                gt3d,
                "fetch_opentopodata_elevations",
                side_effect=AssertionError("cache should satisfy the second resolve"),
            ):
                cached = provider.resolve("Brands Hatch", None, geometry, track_root)

            self.assertEqual(cached.elevations_m, result.elevations_m)


if __name__ == "__main__":
    unittest.main()
