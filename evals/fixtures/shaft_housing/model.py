# shaft_housing — a housing with a plain bore and the customer's shaft passing through it.
P = fc.params()
housing = fc.body("Housing")
fc.pad(housing, fc.sketch(housing, "XY", fc.rect(P.block, P.block), name="BlockSketch"), P.length, name="Block")
fc.holes(housing, on=fc.face(housing, "+Z"), at=[(0, 0)], spec="Ø%g" % P.bore_d, name="Bore")
# the customer's shaft (reference part), on the bore axis and longer than the housing
fc.ref_part("Shaft", shape=Part.makeCylinder(P.shaft_d / 2, P.length + 20, Vector(0, 0, -10)))
