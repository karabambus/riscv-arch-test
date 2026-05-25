# Custom diagnostic tests

Hand-written bare-metal tests, run directly on a core's Verilator testbench.

## cv32e20_umode_interrupt_mie.S

Checks if an M-mode interrupt is taken in U-mode with `mstatus.MIE=0` (required by
Priv ISA sec 3.1.6 / 3.1.9a). Exit value: `0x11` = compliant, `0x01` = MIE-gated
(cv32e20). Targets the cv32e20-dv mm_ram testbench. Build/run notes are in the
file header.
