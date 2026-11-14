/* STM32G0B1. The last 2 KB page of the first 128 KB (0x0801F800) holds the LMU settings
   (SETTINGS_OFFSET in src/main.rs), and the 8 pages below it (0x0801B800-0x0801F7FF) the log
   of the BMCU's charge counter (CHARGE_OFFSET), so the program may only use the first 110 KB.
   Same layout on the NUCLEO-G0B1RE (512 KB): the rest of its flash stays unused. */
MEMORY
{
    FLASH : ORIGIN = 0x08000000, LENGTH = 110K
    RAM   : ORIGIN = 0x20000000, LENGTH = 144K
}
