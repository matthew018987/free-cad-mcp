# drawing_plate — plate with four M4 clearance holes on a 60 × 30 rectangle.
P = fc.params()
plate = fc.body("Plate")
fc.pad(plate, fc.sketch(plate, "XY", fc.rect(P.width, P.depth), name="PlateSketch"), P.thickness, name="Base")
fc.holes(plate, on=fc.face(plate, "+Z"), at=fc.rect_pattern(P.pitch_x, P.pitch_y), spec="M4 clearance normal",
         name="MountHoles")
