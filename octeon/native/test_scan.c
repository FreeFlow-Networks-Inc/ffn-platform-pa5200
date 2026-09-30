/* Test-only native verdict provider. Never installed on an appliance. */
#include <stddef.h>
int test_scan(void *handle, const char *bytes, unsigned size)
{
    if (!handle || !size) return 99;
    unsigned char code = (unsigned char)bytes[size - 1];
    return code == 254 ? -2 : code == 255 ? -1 : code;
}
