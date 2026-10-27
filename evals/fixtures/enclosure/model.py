# enclosure — lid with a centre boss that is meant to press lightly near the PCB.
P = fc.params()

# PCB on its standoffs (fixed by the base — do not move): board + tallest component block
pcb = Part.makeBox(P.pcb_w, P.pcb_d, 1.6, Vector(-P.pcb_w / 2, -P.pcb_d / 2, P.pcb_z))
pcb = pcb.fuse(Part.makeBox(20, 20, P.comp_h, Vector(-10, -10, P.pcb_z + 1.6))).removeSplitter()
fc.ref_part("PCB", shape=pcb)

lid = fc.body("Lid")
fc.pad(lid, fc.sketch(lid, "XY", fc.rect(P.lid_w, P.lid_d), offset=P.lid_z, name="LidSketch"), P.lid_t, name="Plate")
fc.pad(lid, fc.sketch(lid, "XY", fc.circle(d=8, at=(0, 0)), offset=P.lid_z, name="BossSketch"), P.boss_len,
       reverse=True, name="CentreBoss")
