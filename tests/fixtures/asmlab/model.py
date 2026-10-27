# asmlab — Assembly workbench fixture with known answers.
P = fc.params()

base = fc.body("Base")
fc.pad(base, fc.sketch(base, "XY", fc.rect(60, 40), name="BaseSketch"), P.plate_t, name="BasePad")
fc.holes(base, on=fc.face(base, "+Z"), at=[(15, 0)], spec="Ø%g" % P.bore_d, name="Bore")
fc.set_frames(base, {"top": fc.frame(0, 0, P.plate_t, "+Z"), "bore": fc.frame(15, 0, 0, "+Z")})

# reference motor: mounting face at its local z = 0, body toward −Z; starts far away
motor = fc.ref_part("Motor", shape=Part.makeBox(20, 20, 30, Vector(-10, -10, -30)),
                    frames={"mount": fc.frame(0, 0, 0, "+Z")},
                    placement=App.Placement(Vector(300, 200, -50), App.Rotation(Vector(1, 1, 0), 40)))

# shaft with a flat, so the revolute angle is visible: frame at its base, +Z along the axis
shaft_shape = Part.makeCylinder(P.shaft_d / 2, 30).cut(Part.makeBox(10, 10, 30, Vector(3, -5, 0)))
shaft = fc.ref_part("Shaft", shape=shaft_shape, frames={"axis": fc.frame(0, 0, 0, "+Z")})

asm = fc.assembly("Assembly")
fc.add(asm, base, motor, shaft)
fc.ground(asm, base)
fc.joint(asm, "fixed", (base, "top"), (motor, "mount"), mode=P.mount_mode, name="MotorMount")
fc.joint(asm, "revolute", (base, "bore"), (shaft, "axis"), angle=P.spin, name="ShaftSpin")
fc.solve(asm)
