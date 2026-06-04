"""OSM Quick 3D — QGIS plugin entry point.

One-click OpenStreetMap into native QGIS: function-styled 2D layers, flat-roof
3D building massing and a basemap underlay. No browser; built for larger areas.
"""
from .main_plugin import OsmQuick3DPlugin


def classFactory(iface):
    return OsmQuick3DPlugin(iface)
