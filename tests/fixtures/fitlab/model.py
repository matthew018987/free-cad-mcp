# fitlab — geometry with known answers for the M2 tests. Groups sit 100 mm apart in X.
P = fc.params()


def solid(name, shape):
    o = doc.addObject("Part::Feature", name)
    o.Label = name
    o.Shape = shape
    return o


# 1 · interference: overlap 5 × 10 × 10 = 500 mm³
solid("BlockA", Part.makeBox(20, 20, 10))
solid("BlockB", Part.makeBox(10, 10, 10, Vector(15, 5, 0)))

# 2 · a 2.0 mm gap
solid("GapA", Part.makeBox(10, 10, 10, Vector(100, 0, 0)))
solid("GapB", Part.makeBox(10, 10, 10, Vector(112, 0, 0)))

# 3 · plate with a Ø10 bore + 4 × M3 clearance (Ø3.4) on a 31 square, centred (220, 20);
#     shaft Ø9.8 through the bore (radial gap 0.1); flange under the plate (contact at z = 0)
#     with Ø2.5 holes on the same pattern shifted +0.03 in X, and a Ø12 bore
plate = fc.body("Plate")
fc.pad(plate, fc.sketch(plate, "XY", fc.rect(40, 40, center=(220, 20)), name="PlateSketch"), P.plate_t)
fc.holes(plate, on=fc.face(plate, "+Z"), at=[(220, 20)], spec="Ø%g" % P.bore_d, name="Bore")
fc.holes(plate, on=fc.face(plate, "+Z"), at=fc.rect_pattern(P.pattern, P.pattern, center=(220, 20)),
         spec="M3 clearance normal", name="Pattern")
fc.set_frames(plate, {"bottom": fc.frame(220, 20, 0, normal="-Z")})
solid("Shaft", Part.makeCylinder(P.shaft_d / 2, 30, Vector(220, 20, -10)))
fl = Part.makeBox(42, 42, 5, Vector(199, -1, -5))
fl = fl.cut(Part.makeCylinder(6, 20, Vector(220, 20, -10)))
for x, y in fc.rect_pattern(P.pattern, P.pattern, center=(220 + P.pattern_err, 20)):
    fl = fl.cut(Part.makeCylinder(1.25, 20, Vector(x, y, -10)))
fc.ref_part("Flange", shape=fl, frames={"top": fc.frame(220, 20, 0, normal="+Z")})

# 4 · containers and links: global coordinates must compose placements
cont = doc.addObject("App::Part", "Container")
cont.Placement = App.Placement(Vector(0, 0, 50), App.Rotation())
lifted = solid("Lifted", Part.makeBox(10, 10, 10, Vector(300, 0, 0)))
cont.addObject(lifted)
solid("Floor", Part.makeBox(10, 10, 10, Vector(300, 0, -50)))
src = solid("SrcBox", Part.makeBox(5, 5, 5, Vector(400, 0, 0)))
src.Visibility = False
lk = doc.addObject("App::Link", "LinkedBox")
lk.LinkedObject = src
lk.Placement = App.Placement(Vector(0, 0, 100), App.Rotation())
cont.addObject(lk)

# 5 · hole specs from the standards tables
hl = fc.body("HoleLab")
fc.pad(hl, fc.sketch(hl, "XY", fc.rect(80, 30, center=(540, 15)), name="HoleLabSketch"), 10)
top = fc.face(hl, "+Z")
fc.holes(hl, on=top, at=[(510, 15)], spec="M3 insert", name="Insert")
fc.holes(hl, on=fc.face(hl, "+Z"), at=[(525, 15)], spec="M4 clearance normal + cbore DIN974", name="Cbore")
fc.holes(hl, on=fc.face(hl, "+Z"), at=[(545, 15)], spec="Ø8 H7", name="FitBore")
fc.holes(hl, on=fc.face(hl, "+Z"), at=[(565, 15)], spec="M3 clearance normal", name="NutBolt")
fc.nut_trap(hl, on=fc.face(hl, "-Z"), at=[(565, 15)], size="M3", name="NutTrap")
