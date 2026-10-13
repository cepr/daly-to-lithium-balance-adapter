/* STM32G0B1. The last 2 KB page of the first 128 KB (0x0801F800) holds the LMU settings
   (SETTINGS_OFFSET in src/main.rs), so the program may only use the first 126 KB. Same layout
   on the NUCLEO-G0B1RE (512 KB): the rest of its flash stays unused. */
MEMORY
{
    FLASH : ORIGIN = 0x08000000, LENGTH = 126K
    RAM   : ORIGIN = 0x20000000, LENGTH = 144K
}
