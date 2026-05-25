# cv32e20_umode_interrupt_mie.S
#
# Diagnostic: does the core take an M-mode interrupt while running in U-mode
# when mstatus.MIE == 0?
#
# RISC-V Privileged ISA (v20260228):
#   sec 3.1.6  "Interrupts for higher-privilege modes ... are always globally
#               enabled regardless of the setting of the global yIE bit ..."
#   sec 3.1.9  An interrupt traps to M-mode if "(a) either the current privilege
#               mode is M and the MIE bit ... is set, OR the current privilege
#               mode has less privilege than M-mode; ..."
# => In U-mode, a pending+enabled M-interrupt must be taken even if mstatus.MIE==0.
#
# Two phases trigger a machine software interrupt (MSI) from U-mode:
#   Phase A: U-mode, mstatus.MIE = 0  -> spec says TAKEN
#   Phase B: U-mode, mstatus.MIE = 1  -> TAKEN (control; proves the trigger works)
#
# Result is reported in the exit value: (A_taken << 4) | B_taken
#   0x11 = spec-compliant (taken in both)
#   0x01 = MIE-gated in U-mode (cv32e20 behavior: A not taken, B taken)
#
# NOTE: addresses below are specific to the cv32e20-dv mm_ram testbench.
# Build/run instructions are in tests/custom/README.md.

    # --- cv32e20-dv (mm_ram) platform addresses ---
    .equ INT_GEN,    0x15000024     # simple interrupt generator (set/clear reg)
    .equ MSI_SET,    0x80000008     # bit31=set | bit3=MSI
    .equ MSI_CLR,    0x00000008     # bit31=clear | bit3=MSI
    .equ EXIT_ADDR,  0x20000004     # mm_ram MMADDR_EXIT (write -> $finish)
    .equ MPP_MASK,   0x1800         # mstatus.MPP[12:11]
    .equ MPIE_MASK,  0x80           # mstatus.MPIE[7]
    .equ MIE_BIT,    0x8            # mstatus.MIE[3] / mie.MSIE[3]

    .section .text
    .globl _start

_start:
    li   t1, INT_GEN                # clear any stale MSI
    li   t0, MSI_CLR
    sw   t0, 0(t1)

    la   t0, vector_table           # vectored trap base (cv32e20 is vectored-only)
    ori  t0, t0, 1
    csrw mtvec, t0

    li   s0, 0                      # s0 = Phase A taken flag
    li   s1, 0                      # s1 = Phase B taken flag
    li   s2, 0                      # s2 = current phase (0=A, 1=B)

    # ---- Phase A: enter U-mode with MIE = 0 ----
    li   t0, MIE_BIT
    csrw mie, t0                    # mie.MSIE = 1
    csrci mstatus, MIE_BIT          # mstatus.MIE = 0
    li   t0, MPIE_MASK
    csrc mstatus, t0                # MPIE = 0  -> U-mode MIE = 0 after mret
    li   t0, MPP_MASK
    csrc mstatus, t0                # MPP = U
    la   t0, umode_spin
    csrw mepc, t0
    mret

# Shared U-mode body: assert MSI, wait, then leave via ecall.
umode_spin:
    li   t1, INT_GEN
    li   t0, MSI_SET
    sw   t0, 0(t1)
    li   t2, 4000
1:  addi t2, t2, -1
    bnez t2, 1b
    ecall                           # synchronous -> vector base (offset 0)

    .balign 256                     # cv32e20 MTVEC_BASE_ALIGNMENT_VECTORED = 256
vector_table:
    jal  x0, exc_handler            # +0   exceptions (incl. U-mode ecall)
    jal  x0, bad                    # +4   cause 1
    jal  x0, bad                    # +8   cause 2
    jal  x0, msi_handler            # +12  cause 3 = machine software interrupt
    jal  x0, bad                    # +16  cause 4
    jal  x0, bad                    # +20  cause 5
    jal  x0, bad                    # +24  cause 6
    jal  x0, bad                    # +28  cause 7 = machine timer
    jal  x0, bad                    # +32  cause 8
    jal  x0, bad                    # +36  cause 9
    jal  x0, bad                    # +40  cause 10
    jal  x0, bad                    # +44  cause 11 = machine external

# MSI taken: record for the current phase, clear it, return to U-mode.
msi_handler:
    bnez s2, 1f
    li   s0, 1
    j    2f
1:  li   s1, 1
2:  li   t1, INT_GEN
    li   t0, MSI_CLR
    sw   t0, 0(t1)
    mret

# Reached when the U-mode spin finishes (via ecall) without the MSI being taken.
exc_handler:
    li   t1, INT_GEN                # drop any still-pending MSI
    li   t0, MSI_CLR
    sw   t0, 0(t1)
    bnez s2, report

    # ---- Phase B: enter U-mode with MIE = 1 ----
    li   s2, 1
    li   t0, MIE_BIT
    csrw mie, t0
    csrci mstatus, MIE_BIT
    li   t0, MPIE_MASK
    csrs mstatus, t0                # MPIE = 1 -> U-mode MIE = 1 after mret
    li   t0, MPP_MASK
    csrc mstatus, t0                # MPP = U
    la   t0, umode_spin
    csrw mepc, t0
    mret

report:
    slli t0, s0, 4
    or   t0, t0, s1                 # exit_value = (A_taken<<4) | B_taken
    li   t1, EXIT_ADDR
    sw   t0, 0(t1)
3:  j 3b

bad:                                # unexpected vector
    li   t0, 0xBAD
    li   t1, EXIT_ADDR
    sw   t0, 0(t1)
4:  j 4b
