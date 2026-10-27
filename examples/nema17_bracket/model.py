# nema17_bracket — model.py
# L-bracket: a vertical plate carries the motor (motor body in front, −Y; shaft through the
# plate toward +Y); a base plate behind it screws to the frame at z = 0.
# Units mm. Z up; the front view looks toward +Y.

P = fc.params()

# ---- reference part: NEMA 17 envelope with its true interface geometry -------------
# Built in its own frame: mounting face at z = 0, body toward −Z, pilot + shaft toward +Z.
f = P.motor_flange
motor_shape = Part.makeBox(f, f, P.motor_length, Vector(-f / 2, -f / 2, -P.motor_length))
motor_shape = motor_shape.fuse(Part.makeCylinder(P.motor_pilot_d / 2, P.motor_pilot_h))
motor_shape = motor_shape.fuse(Part.makeCylinder(P.shaft_d / 2, P.shaft_len))
for x, y in fc.rect_pattern(P.motor_hole_pitch, P.motor_hole_pitch):        # M3 tapped, 4.5 deep
    motor_shape = motor_shape.cut(Part.makeCylinder(1.25, 4.5, Vector(x, y, -4.5)))
motor = fc.ref_part("Motor", shape=motor_shape.removeSplitter(),
                    frames={"mount": fc.frame(0, 0, 0, normal="+Z")})

# ---- the bracket ---------------------------------------------------------------------
body = fc.body("Bracket")

# base plate on the frame (z = 0 … wall), behind the motor plate (y = 0 … base_depth)
base = fc.pad(body, fc.sketch(body, "XY", fc.rect(P.plate_w, P.base_depth, center=(0, P.base_depth / 2)),
                              name="BaseSketch"), P.wall, name="Base")

# vertical plate: y = 0 … wall, z = 0 … plate_h  (XZ sketch, padded toward +Y)
plate = fc.pad(body, fc.sketch(body, "XZ", fc.rect(P.plate_w, P.plate_h, center=(0, P.plate_h / 2)),
                               name="PlateSketch"), P.wall, reverse=True, name="Plate")

# two gussets tying plate to base, just inside the side edges
for side in (-1, 1):
    x = side * (P.plate_w / 2 - P.gusset_t / 2)
    tri = fc.polygon([(x, P.wall, P.wall), (x, P.wall + P.gusset, P.wall), (x, P.wall, P.wall + P.gusset)])
    fc.pad(body, fc.sketch(body, "YZ", tri, offset=x, name="GussetSketch"), P.gusset_t,
           symmetric=True, name="Gusset")

# motor interface, dimensioned from the motor axis (design intent), on the front face
front = fc.face(body, "-Y")
axis = (0, P.axis_z)                                                      # (x, z) on the −Y face
fc.holes(body, on=front, at=[axis], spec="Ø%g" % (P.motor_pilot_d + P.pilot_clear), name="PilotBore")
fc.holes(body, on=fc.face(body, "-Y"),
         at=fc.rect_pattern(P.motor_hole_pitch, P.motor_hole_pitch, center=axis),
         spec="M3 clearance normal", name="MotorScrews")

# frame screws through the base (5 mm is too thin to counterbore an M4 socket head)
fc.holes(body, on=fc.face(body, "+Z", at=P.wall),
         at=[(-P.frame_screw_x, P.frame_screw_y), (P.frame_screw_x, P.frame_screw_y)],
         spec="M4 clearance normal", name="FrameScrews")

# ---- assemble: motor face against the plate's front face, on the axis ----------------
fc.mate(motor, "mount", fc.frame_on(fc.face(body, "-Y"), at=(0, 0, P.axis_z)), mode="against")
