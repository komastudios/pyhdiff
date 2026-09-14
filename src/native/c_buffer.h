#ifndef PHD_C_BUFFER_H
#define PHD_C_BUFFER_H
#include <stddef.h>
#include <stdlib.h>
/* C FFI adapter. The caller owns a buffer of size bytes. Check every slice
 * before pointer arithmetic. An internal range defect is a fatal error. */
static inline const unsigned char *phd_at(const unsigned char *p, size_t size,
                                          size_t offset, size_t count) {
    if (offset > size || count > size - offset)
        abort();
#pragma clang unsafe_buffer_usage begin
    return p + offset;
#pragma clang unsafe_buffer_usage end
}
static inline unsigned char *phd_mut(unsigned char *p, size_t size,
                                     size_t offset, size_t count) {
    return (unsigned char *)phd_at(p, size, offset, count);
}
#endif
