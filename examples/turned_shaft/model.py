# turned_shaft — model.py. Axis along Z, collar at z = 0.
P = fc.params()

shaft = fc.body("Shaft")
fc.turned(shaft, [(P.collar_d, P.collar_l), (P.seat_d, P.seat_l, "k6"), (P.thread_d, P.thread_l)], name="Profile")
# chamfer the outer edges of every step
fc.chamfer(shaft, fc.edges(shaft, kind="circle", r=P.thread_d / 2), P.chamfer, name="ThreadChamfer")
fc.chamfer(shaft, fc.edges(shaft, kind="circle", r=P.collar_d / 2), P.chamfer, name="CollarChamfer")
fc.external_thread(shaft, "M8", P.thread_l, z_from=P.collar_l + P.seat_l)
# cross hole for a roll pin through the collar, drilled from a plane just outside it
fc.holes(shaft, on="XZ", offset=P.collar_d / 2 + 1, at=[(0, P.collar_l / 2)],
         spec="Ø%g" % P.pin_d, depth="through", name="PinHole")
