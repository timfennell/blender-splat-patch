# Gaussian Splat Patch (Blender 5.3+)

Erase, clone stamp and heal 3D Gaussian splat scans inside Blender, using Blender 5.3's
native splat import (`File > Import > PLY / SPZ`). Built for cleaning insect specimens:
dust, stray hairs, and the pin and the hole it leaves.

Sidebar: **3D Viewport > N > Splat Patch**.

## Install

Download `gs_patch-*.zip` from [Releases](https://github.com/timfennell/blender-splat-patch/releases),
then in Blender use **Edit > Preferences > Get Extensions > ⌄ > Install from Disk…** and pick the zip.
Requires Blender 5.3 or newer.

## Tools (brush)

Pick a tool button to start painting. While the brush is running:

| Key | Action |
|---|---|
| LMB drag | paint with the current tool |
| Ctrl+LMB | Clone/Heal: set the sample point · Select: deselect |
| `[` `]` | brush radius · Shift: feather |
| `X` | Surface ↔ Through depth (Through reaches every depth, e.g. a whole pin) |
| `1`–`5` | Erase / Select / Clone / Heal / Spot Heal |
| Esc / Enter | stop the brush |

MMB, the scroll wheel and trackpad gestures still navigate the view. Each stroke is one undo step.

- **Erase**: deletes splats; the feather fades them instead. Erase, Select and Spot Heal also work over
  empty space: with nothing solid under the brush (a white circle), they take everything under the
  circle at any depth. Needle-shaped splats count wherever their length crosses the brush, not just at
  their centre.
- **Select**: paints a region to use with the Selection & Fill buttons.
- **Clone**: works like Photoshop's clone stamp. Ctrl+click a clean area, then paint over the defect. The splats
  are rotated to fit the target surface's normal, including their view-dependent colour (SH bands 1–3),
  so highlights and sheen turn with the surface. *Aligned* keeps the offset between strokes.
  *Replace Destination* removes what was there, crossfading across the feather.
- **Heal**: clones like Clone, then shifts the colour to match the ring around the destination.
- **Spot Heal**: paint over a speck; it is deleted and new splats are grown from the surrounding surface.

## Selection & Fill (the sample / edit / fill volumes)

1. Select the volume to remove: paint with **Select**, use **Select Around Cursor** (sphere at the 3D
   cursor), **Select Faint Splats** (near-transparent haze), or **Select Floaters**. Floaters are clumps
   not connected to the specimen, such as wisps left where a pin was. *Gap* sets how far apart splats can
   be and still count as connected (0 = automatic). *Keep* protects clumps at least that fraction of the
   largest one, such as a detached leg.
2. **Delete Selected**, or
3. **Fill From Surroundings** (spot-heal the selection), or **Fill From Sample**: clone the area around
   the sample point into the gap, trimmed to the gap's footprint, feathered and colour-matched.

The fill fits a smooth surface to the ring of splats around the gap, finds the gap in that surface, and
fills it to the ring's density. Heal Settings (Border, Grain, Colour Smoothing, Density) tune it.

## Export

Blender 5.3's PLY exporter writes 0 points for a point cloud, so use **Export > Export Splat PLY**.
It writes the standard INRIA 3DGS layout that Brush, SuperSplat and Blender read. A round trip matches
the original file exactly. Positions are written in object space (the object transform is ignored).

## Limits

- The brush reads the whole cloud when it starts and each stroke is applied on release, so very large
  scans (millions of splats) take a moment per stroke.

## Development

```
blender --command extension build --source-dir gs_patch --output-dir dist
blender --command extension install-file -r user_default -e dist/gs_patch-0.1.0.zip
```
