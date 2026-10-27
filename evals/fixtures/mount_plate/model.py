# mount_plate — simple plate with two M4 clearance holes.
P = fc.params()
plate = fc.body("Plate")
fc.pad(plate, fc.sketch(plate, "XY", fc.rect(P.width, P.depth), name="PlateSketch"), P.thickness, name="Base")
fc.holes(plate, on=fc.face(plate, "+Z"), at=[(-P.pitch_x / 2, 0), (P.pitch_x / 2, 0)],
         spec="M4 clearance normal", name="MountHoles")
