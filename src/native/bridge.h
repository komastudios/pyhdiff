#ifndef PHD_BRIDGE_H
#define PHD_BRIDGE_H
#include <stddef.h>
#include <stdint.h>
/* Codec inputs and reconstructions. */
#define PHD_RAW_MAX ((size_t)256 * 1024 * 1024)
/* Encoded payloads. Leaves room for a 96-byte envelope within 512 MiB. */
#define PHD_PAYLOAD_MAX ((size_t)512 * 1024 * 1024 - 96)
#define PHD_STEP_MAX ((size_t)256 * 1024)
#define PHD_HDIFF_WINDOW_LOG_MAX 23
#define PHD_ZSTD_WINDOW_LOG_MAX 30
/* Standalone frames: input and output limit of compress and decompress. */
#define PHD_FRAME_MAX ((size_t)1024 * 1024 * 1024)
#ifdef __cplusplus
#define PHD_NOEXCEPT noexcept
extern "C" {
#else
#define PHD_NOEXCEPT
#endif
/* Borrowed inputs. A successful result owns malloc storage, released with free.
 * Failure results contain no allocation. No function lets a C++ exception
 * escape. Messages and framing belong to the caller. */
typedef enum {
    PHD_OK,
    PHD_INVALID,   /* malformed payload, or a mismatch with the stated sizes */
    PHD_LIMIT,     /* a size limit was exceeded */
    PHD_ALLOC,     /* allocation failed */
    PHD_EXCEPTION, /* standard exception */
    PHD_UNKNOWN,   /* unknown exception */
    PHD_OPTION,    /* profile parameter out of range */
    PHD_CANCELLED, /* fault injection builds only */
    PHD_ZSTD_ERROR_BASE = 1000,
    PHD_ZSTD_ERROR_LAST = 1999
} phd_status;
typedef struct {
    unsigned char *data;
    size_t size;
    phd_status status;
} phd_result;
/* HDiffPatch single compressed diff with zstd compression. Booleans are 0/1. */
typedef struct {
    int match_score, level, window_log, fast_block_bytes, step_bytes;
    int content_size, checksum;
} phd_hdiff_options;
#define PHD_HDIFF_DEFAULT {6, 19, 23, 1024, 262144, 1, 0}
/* One zstd frame. A nonempty base is referenced as a prefix. ldm: 0 automatic,
 * 1 on, 2 off. Zero selects automatic tuning for the window and LDM values. */
typedef struct {
    int level, window_log, ldm, content_size, checksum;
    int ldm_hash_log, ldm_min_match, ldm_bucket_log, ldm_rate_log;
} phd_zstd_options;
#define PHD_ZSTD_DEFAULT {3, 0, 0, 1, 1, 0, 0, 0, 0}
phd_result phd_hdiff_encode(const unsigned char *base, size_t base_size,
                            const unsigned char *target, size_t target_size,
                            const phd_hdiff_options *) PHD_NOEXCEPT;
/* The result has exactly target_size bytes, or the call fails. */
phd_result phd_hdiff_apply(const unsigned char *base, size_t base_size,
                           const unsigned char *payload, size_t payload_size,
                           size_t target_size) PHD_NOEXCEPT;
phd_result phd_zstd_encode(const unsigned char *base, size_t base_size,
                           const unsigned char *target, size_t target_size,
                           const phd_zstd_options *) PHD_NOEXCEPT;
/* Exactly one frame with a window of at most 2^30 bytes, and nothing else. */
phd_result phd_zstd_apply(const unsigned char *base, size_t base_size,
                          const unsigned char *payload, size_t payload_size,
                          size_t target_size) PHD_NOEXCEPT;
/* Standard zstd frames without a base. window_log 0 selects the level
 * default. The frame records the content size. */
phd_result phd_zstd_compress(const unsigned char *data, size_t size, int level,
                             int window_log, int checksum) PHD_NOEXCEPT;
/* Decodes one or more concatenated frames, skippable frames included. Fails
 * with PHD_LIMIT as soon as the output would exceed max_output. */
phd_result phd_zstd_decompress(const unsigned char *data, size_t size,
                               size_t max_output) PHD_NOEXCEPT;
#ifdef PHD_TEST
/* Fault injection for the native test build only. */
typedef int (*phd_test_callback)(void *);
phd_result phd_test_zstd_encode(const unsigned char *, size_t,
                                const unsigned char *, size_t,
                                const phd_zstd_options *, phd_test_callback,
                                void *) PHD_NOEXCEPT;
phd_result phd_test_zstd_apply(const unsigned char *, size_t,
                               const unsigned char *, size_t, size_t,
                               phd_test_callback, void *) PHD_NOEXCEPT;
phd_result phd_test_hdiff_wiring(void) PHD_NOEXCEPT;
phd_result phd_test_zstd_allocation(void) PHD_NOEXCEPT;
phd_result phd_test_exception(int) PHD_NOEXCEPT;
phd_result phd_test_plugin_throw(void) PHD_NOEXCEPT;
#endif
#ifdef __cplusplus
}
#endif
#endif
