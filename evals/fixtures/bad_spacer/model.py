# bad_spacer — printed spacer.
P = fc.params()
s = fc.body("Spacer")
fc.pad(s, fc.sketch(s, "XY", fc.circle(d=P.od), name="SpacerSketch"), P.height, name="Ring")
fc.holes(s, on=fc.face(s, "+Z"), at=[(0, 0)], spec="Ø%g" % P.bore, name="Bore")
