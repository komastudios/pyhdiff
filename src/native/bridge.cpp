#include "bridge.h"
#include "libHDiffPatch/HDiff/diff.h"
#include "libHDiffPatch/HPatch/patch.h"
#include <algorithm>
#include <array>
#include <cstdlib>
#include <cstring>
#include <memory>
#include <new>
#include <span>
#include <stdexcept>
#include <type_traits>
#include <vector>
#define ZSTD_STATIC_LINKING_ONLY
#include <zstd.h>
#include <zstd_errors.h>

namespace {
using Bytes = std::span<const unsigned char>;
using Mutable = std::span<unsigned char>;
using Cancel = int (*)(void *);
struct Failure {
    phd_status status;
};
void require(bool condition, phd_status status = PHD_INVALID) {
    if (!condition)
        throw Failure{status};
}
void zcheck(size_t result) {
    if (ZSTD_isError(result))
        throw Failure{ZSTD_getErrorCode(result) == ZSTD_error_memory_allocation
                          ? PHD_ALLOC : static_cast<phd_status>(static_cast<int>(PHD_ZSTD_ERROR_BASE) + static_cast<int>(ZSTD_getErrorCode(result)))};
}
// The measured codec paths share one option set. The public structs map onto
// it after validation.
struct Options {
    int level, window_log, workers, content_size, checksum;
    int match_score, fast_block_bytes, step_bytes;
    int ldm, ldm_hash_log, ldm_min_match, ldm_bucket_log, ldm_rate_log;
};
void zset(ZSTD_CCtx *ctx, ZSTD_cParameter parameter, int value) {
    zcheck(ZSTD_CCtx_setParameter(ctx, parameter, value));
#ifdef PHD_TEST
    int actual = 0;
    zcheck(ZSTD_CCtx_getParameter(ctx, parameter, &actual));
    require(actual == value);
#endif
}


#ifdef PHD_TEST
void verify_options(ZSTD_CCtx *ctx, const Options &o, bool hdiff) {
    auto same = [&](ZSTD_cParameter parameter, int expected) {
        int actual = 0;
        zcheck(ZSTD_CCtx_getParameter(ctx, parameter, &actual));
        require(actual == expected);
    };
    same(ZSTD_c_compressionLevel, o.level);
    same(ZSTD_c_nbWorkers, o.workers);
    same(ZSTD_c_contentSizeFlag, o.content_size);
    same(ZSTD_c_checksumFlag, o.checksum);
    if (o.window_log) same(ZSTD_c_windowLog, o.window_log);
    if (!hdiff) {
        if (o.ldm) same(ZSTD_c_enableLongDistanceMatching, o.ldm);
        if (o.ldm_hash_log) same(ZSTD_c_ldmHashLog, o.ldm_hash_log);
        else if (o.ldm_bucket_log) {
            int hash = 0;
            zcheck(ZSTD_CCtx_getParameter(ctx, ZSTD_c_ldmHashLog, &hash));
            require(hash >= o.ldm_bucket_log);
        }
        if (o.ldm_min_match) same(ZSTD_c_ldmMinMatch, o.ldm_min_match);
        if (o.ldm_bucket_log) same(ZSTD_c_ldmBucketSizeLog, o.ldm_bucket_log);
        if (o.ldm_rate_log) same(ZSTD_c_ldmHashRateLog, o.ldm_rate_log);
    }
}
#endif

/* Audited FFI adapters: pointer/length pairs come from C or HDiffPatch's
 * stream contract. All range checks occur before these adapters are called. */
#pragma clang unsafe_buffer_usage begin
Bytes borrowed(const unsigned char *p, size_t n) { return {p, n}; }
Mutable borrowed(unsigned char *p, size_t n) { return {p, n}; }
Bytes range(const unsigned char *b, const unsigned char *e) { return {b, e}; }
Mutable range(unsigned char *b, unsigned char *e) { return {b, e}; }
const unsigned char *end(Bytes b) { return b.data() + b.size(); }
unsigned char *end(Mutable b) { return b.data() + b.size(); }
bool is_zstd(const char *type) { return std::strcmp(type, "zstd") == 0; }
#pragma clang unsafe_buffer_usage end

struct Free {
    void operator()(unsigned char *p) const noexcept { std::free(p); }
};
using Owned = std::unique_ptr<unsigned char, Free>;
/* malloc storage that grows without zero-filling, so reserved pages are only
 * committed when written, and that transfers to the caller without a copy. */
class Growable {
  public:
    explicit Growable(size_t reserve = 0) {
        if (reserve)
            grow_to(reserve);
    }
    size_t size() const noexcept { return size_; }
    Mutable bytes() noexcept { return borrowed(data_.get(), size_); }
    // Uninitialized storage past size(), at least `minimum` bytes of it.
    Mutable spare(size_t minimum) {
        if (capacity_ - size_ < minimum)
            grow_to(std::max(size_ + minimum, capacity_ + capacity_ / 2));
        return borrowed(data_.get(), capacity_).subspan(size_);
    }
    // Callers overwrite every byte between the old and the new size.
    void resize(size_t n) {
        if (n > size_)
            spare(n - size_);
        size_ = n;
    }
    void append(Bytes b) {
        std::ranges::copy(b, spare(b.size()).begin());
        size_ += b.size();
    }
    phd_result release() {
        if (!data_)
            grow_to(1);
        return {data_.release(), size_, PHD_OK};
    }

  private:
    void grow_to(size_t n) {
        auto *p = static_cast<unsigned char *>(std::realloc(data_.get(), n));
        if (!p)
            throw std::bad_alloc();
        (void)data_.release();
        data_.reset(p);
        capacity_ = n;
    }
    Owned data_;
    size_t size_ = 0, capacity_ = 0;
};
template <class F> phd_result boundary(F f) noexcept {
    try {
        return f();
    } catch (const std::bad_alloc &) {
        return {nullptr, 0, PHD_ALLOC};
    } catch (const Failure &e) {
        return {nullptr, 0, e.status};
    } catch (const std::exception &) {
        return {nullptr, 0, PHD_EXCEPTION};
    } catch (...) {
        return {nullptr, 0, PHD_UNKNOWN};
    }
}
struct Input {
    hpatch_TStreamInput stream{};
    Bytes bytes;
    explicit Input(Bytes b) : bytes(b) {
        stream.streamImport = this;
        stream.streamSize = b.size();
        stream.read = [](const hpatch_TStreamInput *s, hpatch_StreamPos_t pos,
                         unsigned char *b, unsigned char *e) -> hpatch_BOOL {
            auto &self = *static_cast<const Input *>(s->streamImport);
            auto dst = range(b, e);
            if (pos > self.bytes.size() || dst.size() > self.bytes.size() - pos)
                return hpatch_FALSE;
            auto src = self.bytes.subspan(static_cast<size_t>(pos), dst.size());
            std::copy(src.begin(), src.end(), dst.begin());
            return hpatch_TRUE;
        };
    }
};
struct Output {
    hpatch_TStreamOutput stream{};
    Growable bytes;
    explicit Output(size_t limit) {
        stream.streamImport = this;
        stream.streamSize = limit;
        stream.write = [](const hpatch_TStreamOutput *s, hpatch_StreamPos_t pos,
                          const unsigned char *b,
                          const unsigned char *e) -> hpatch_BOOL {
            auto &self = *static_cast<Output *>(s->streamImport);
            auto src = range(b, e);
            require(pos <= s->streamSize && src.size() <= s->streamSize - pos,
                    PHD_LIMIT);
            require(pos <= self.bytes.size());
            size_t finish = static_cast<size_t>(pos) + src.size();
            if (finish > self.bytes.size())
                self.bytes.resize(finish);
            auto dst = self.bytes.bytes()
                           .subspan(static_cast<size_t>(pos), src.size());
            std::copy(src.begin(), src.end(), dst.begin());
            return hpatch_TRUE;
        };
    }
};
using Compressor = std::unique_ptr<ZSTD_CCtx, decltype(&ZSTD_freeCCtx)>;
using Decoder = std::unique_ptr<ZSTD_DCtx, decltype(&ZSTD_freeDCtx)>;

struct Compress {
    hdiff_TCompress plugin;
    Options options;
};
static_assert(std::is_standard_layout_v<Compress>);
hpatch_StreamPos_t compress(const hdiff_TCompress *plugin,
                            const hpatch_TStreamOutput *out,
                            const hpatch_TStreamInput *in) {
    const auto &options = reinterpret_cast<const Compress *>(plugin)->options;
    Compressor ctx(ZSTD_createCCtx(), ZSTD_freeCCtx);
    if (!ctx)
        throw std::bad_alloc();
    std::vector<unsigned char> input(ZSTD_CStreamInSize());
    std::vector<unsigned char> output(ZSTD_CStreamOutSize());
    zset(ctx.get(), ZSTD_c_compressionLevel, options.level);
    zset(ctx.get(), ZSTD_c_windowLog, options.window_log);
    zset(ctx.get(), ZSTD_c_nbWorkers, 0);
    zset(ctx.get(), ZSTD_c_contentSizeFlag, options.content_size);
    zset(ctx.get(), ZSTD_c_checksumFlag, options.checksum);
#ifdef PHD_TEST
    verify_options(ctx.get(), options, true);
#endif
    zcheck(ZSTD_CCtx_setPledgedSrcSize(ctx.get(), in->streamSize));
    hpatch_StreamPos_t read = 0, written = 0;
    do {
        size_t n = static_cast<size_t>(
            std::min<hpatch_StreamPos_t>(input.size(), in->streamSize - read));
        auto chunk = Mutable(input).first(n);
        require(in->read(in, read, chunk.data(), end(chunk)));
        read += n;
        ZSTD_inBuffer ib{chunk.data(), n, 0};
        bool last = read == in->streamSize;
        size_t remaining;
        do {
            ZSTD_outBuffer ob{output.data(), output.size(), 0};
            remaining = ZSTD_compressStream2(
                ctx.get(), &ob, &ib, last ? ZSTD_e_end : ZSTD_e_continue);
            zcheck(remaining);
            auto encoded = Bytes(output).first(ob.pos);
            // HDiffPatch may abandon compression when it cannot reduce size.
            if (!out->write(out, written, encoded.data(), end(encoded)))
                return 0;
            written += ob.pos;
        } while (last ? remaining != 0 : ib.pos != ib.size);
        if (last)
            return written;
    } while (true);
}
const hdiff_TCompress plugin_template{
    [](void) -> const char * { return "zstd"; },
    [](hpatch_StreamPos_t n) -> hpatch_StreamPos_t { return n + n / 8 + 1024; },
    [](hdiff_TCompress *, int) -> int { return 1; }, compress, nullptr};

/* The state owns the decoder for the entire patch call, including unwinding.
 * The plugin has no owning handle. */
struct Decompress {
    hpatch_TDecompress plugin{};
    Decoder ctx{nullptr, ZSTD_freeDCtx};
    std::array<unsigned char, 65536> buffer{};
    const hpatch_TStreamInput *source = nullptr;
    hpatch_StreamPos_t pos = 0, stop = 0, remaining = 0;
    ZSTD_inBuffer input{};
    bool finished = false;
    Decompress() {
        plugin.is_can_open = [](const char *type) -> hpatch_BOOL {
            return is_zstd(type);
        };
        plugin.open = [](hpatch_TDecompress *p, hpatch_StreamPos_t size,
                         const hpatch_TStreamInput *s, hpatch_StreamPos_t b,
                         hpatch_StreamPos_t e) -> hpatch_decompressHandle {
            auto &self = *reinterpret_cast<Decompress *>(p);
            require(b <= e && e <= s->streamSize && size <= PHD_PAYLOAD_MAX);
            self.ctx.reset(ZSTD_createDCtx());
            if (!self.ctx)
                throw std::bad_alloc();
            zcheck(ZSTD_DCtx_setParameter(self.ctx.get(), ZSTD_d_windowLogMax,
                                          PHD_HDIFF_WINDOW_LOG_MAX));
            self.source = s;
            self.pos = b;
            self.stop = e;
            self.remaining = size;
            self.input = {self.buffer.data(), 0, 0};
            self.finished = false;
            return &self;
        };
        plugin.decompress_part = [](void *handle, unsigned char *b,
                                    unsigned char *e) -> hpatch_BOOL {
            auto &self = *static_cast<Decompress *>(handle);
            auto dst = range(b, e);
            require(dst.size() <= self.remaining);
            ZSTD_outBuffer ob{dst.data(), dst.size(), 0};
            while (ob.pos < ob.size) {
                require(!self.finished);
                self.refill();
                size_t before_in = self.input.pos, before_out = ob.pos;
                size_t r =
                    ZSTD_decompressStream(self.ctx.get(), &ob, &self.input);
                zcheck(r);
                self.finished = r == 0;
                require(before_in != self.input.pos || before_out != ob.pos);
            }
            self.remaining -= dst.size();
            return hpatch_TRUE;
        };
        plugin.close = [](hpatch_TDecompress *, void *handle) -> hpatch_BOOL {
            auto &self = *static_cast<Decompress *>(handle);
            if (self.remaining)
                return hpatch_FALSE;
            // Consume the frame trailer without accepting additional output.
            while (!self.finished) {
                self.refill();
                unsigned char extra = 0;
                ZSTD_outBuffer ob{&extra, 1, 0};
                size_t before = self.input.pos;
                size_t r =
                    ZSTD_decompressStream(self.ctx.get(), &ob, &self.input);
                zcheck(r);
                require(ob.pos == 0);
                self.finished = r == 0;
                require(self.finished || before != self.input.pos);
            }
            return self.pos == self.stop && self.input.pos == self.input.size;
        };
    }
    void refill() {
        if (input.pos != input.size || pos == stop)
            return;
        size_t n = static_cast<size_t>(
            std::min<hpatch_StreamPos_t>(buffer.size(), stop - pos));
        auto dst = Mutable(buffer).first(n);
        require(source->read(source, pos, dst.data(), end(dst)));
        pos += n;
        input = {buffer.data(), n, 0};
    }
};
static_assert(std::is_standard_layout_v<Decompress>);

template <class Invoke>
Growable hdiff_encode(Bytes base, Bytes target,
                                        const Options &options, Invoke invoke) {
    require(base.size() <= PHD_RAW_MAX && target.size() <= PHD_RAW_MAX,
            PHD_LIMIT);
    Compress compressor{plugin_template, options};
    Input old(base), next(target);
    Output out(PHD_PAYLOAD_MAX);
    hdiff_TMTSets_s mt{1, 1, false, false};
    invoke(&next.stream, &old.stream, &out.stream, &compressor.plugin,
           options.step_bytes, options.fast_block_bytes, options.match_score,
           false, &mt);
    return std::move(out.bytes);
}
void hdiff_apply(Bytes base, Bytes patch, Mutable dst) {
    size_t bn = base.size(), pn = patch.size(), tn = dst.size();
    require(bn <= PHD_RAW_MAX && tn <= PHD_RAW_MAX && pn <= PHD_PAYLOAD_MAX,
            PHD_LIMIT);
    Input old(base), delta(patch);
    hpatch_singleCompressedDiffInfo info{};
    require(getSingleCompressedDiffInfo(&info, &delta.stream, 0));
    require(info.oldDataSize == bn && info.newDataSize == tn);
    require(info.stepMemSize <= PHD_STEP_MAX &&
                info.uncompressedSize <= PHD_PAYLOAD_MAX,
            PHD_LIMIT);
    require(info.compressType[0] == 0 || is_zstd(info.compressType));
    auto encoded =
        info.compressedSize ? info.compressedSize : info.uncompressedSize;
    require(info.diffDataPos <= pn && encoded == pn - info.diffDataPos);
    require(info.compressedSize == 0 || info.compressType[0] != 0);
    hpatch_TStreamOutput output{};
    mem_as_hStreamOutput(&output, dst.data(), end(dst));
    std::vector<unsigned char> cache(static_cast<size_t>(info.stepMemSize) +
                                     hpatch_kStreamCacheSize * 3);
    Decompress decoder;
    require(patch_single_compressed_diff(
        &output, &old.stream, &delta.stream, info.diffDataPos,
        info.uncompressedSize, info.compressedSize,
        info.compressedSize ? &decoder.plugin : nullptr, info.coverCount,
        static_cast<size_t>(info.stepMemSize), cache.data(),
        end(Mutable(cache)), nullptr, 1));
}

struct Limits {
    size_t input, output;
};
constexpr Limits kCodecLimits{PHD_RAW_MAX, PHD_PAYLOAD_MAX};
constexpr Limits kFrameLimits{PHD_FRAME_MAX, PHD_FRAME_MAX};
Growable zstd_encode(Bytes base, Bytes target,
                                       const Options &o, Limits limits,
                                       Cancel cancel, void *opaque) {
    size_t bn = base.size(), tn = target.size();
    require(bn <= PHD_RAW_MAX && tn <= limits.input, PHD_LIMIT);
    Compressor ctx(ZSTD_createCCtx(), ZSTD_freeCCtx);
    if (!ctx) throw std::bad_alloc();
    auto set = [&](ZSTD_cParameter p, int v) {
        zset(ctx.get(), p, v);
    };
    set(ZSTD_c_compressionLevel, o.level);
    set(ZSTD_c_nbWorkers, o.workers);
    set(ZSTD_c_contentSizeFlag, o.content_size);
    set(ZSTD_c_checksumFlag, o.checksum);
    int window = o.window_log, ldm = o.ldm;
    if (bn && !window) {
        auto p = ZSTD_getCParams(o.level, tn, bn);
        unsigned log = 0;
        for (size_t n = tn; n; n >>= 1) ++log;
        window = static_cast<int>(std::clamp(log, 10u, 30u));
        if (!ldm && log > p.chainLog - (p.strategy >= ZSTD_btlazy2)) ldm = 1;
    } else if (bn && !ldm) ldm = 1;
    if (window) set(ZSTD_c_windowLog, window);
    if (ldm) set(ZSTD_c_enableLongDistanceMatching, ldm);
    int hash_log = o.ldm_hash_log;
    if (!hash_log && (o.ldm_bucket_log || o.ldm_rate_log)) {
        // Honor an explicit bucket size even for very small sources.
        // zstd otherwise silently reduces it to the automatic hash log.
        // Resolve explicit rates with signed arithmetic too: zstd's
        // unsigned window-minus-rate can underflow for small windows.
        auto p = ZSTD_getCParams(o.level, tn, bn);
        p.windowLog = window ? static_cast<unsigned>(window) : 27u;
        p = ZSTD_adjustCParams(p, std::max(tn, size_t{1}), bn);
        int rate = o.ldm_rate_log ? o.ldm_rate_log : 7 - static_cast<int>(p.strategy) / 3;
        hash_log = std::max(o.ldm_bucket_log,
            std::clamp(static_cast<int>(p.windowLog) - rate, 6, 30));
    }
    if (hash_log) set(ZSTD_c_ldmHashLog, hash_log);
    if (o.ldm_min_match) set(ZSTD_c_ldmMinMatch, o.ldm_min_match);
    if (o.ldm_bucket_log) set(ZSTD_c_ldmBucketSizeLog, o.ldm_bucket_log);
    if (o.ldm_rate_log) set(ZSTD_c_ldmHashRateLog, o.ldm_rate_log);
#ifdef PHD_TEST
    verify_options(ctx.get(), o, false);
#endif
    zcheck(ZSTD_CCtx_setPledgedSrcSize(ctx.get(), tn));
    zcheck(ZSTD_CCtx_refPrefix(ctx.get(), base.data(), bn));
    std::array<unsigned char, 65536> buffer{};
    Growable encoded;
    size_t pos = 0, remaining;
    do {
        auto chunk = target.subspan(pos, std::min(size_t{65536}, tn - pos));
        ZSTD_inBuffer ib{chunk.data(), chunk.size(), 0};
        bool last = pos + chunk.size() == tn;
        do {
            require(!cancel || !cancel(opaque), PHD_CANCELLED);
            ZSTD_outBuffer ob{buffer.data(), buffer.size(), 0};
            remaining = ZSTD_compressStream2(ctx.get(), &ob, &ib,
                last ? ZSTD_e_end : ZSTD_e_continue);
            zcheck(remaining);
            require(ob.pos <= limits.output - encoded.size(), PHD_LIMIT);
            encoded.append(Bytes(buffer).first(ob.pos));
        } while (last ? remaining != 0 : ib.pos != ib.size);
        pos += chunk.size();
    } while (pos != tn);
    return encoded;
}

void zstd_apply(Bytes base, Bytes src, Mutable dst, Cancel cancel,
                void *opaque) {
    size_t bn = base.size(), dn = src.size(), tn = dst.size();
    const int window = PHD_ZSTD_WINDOW_LOG_MAX;
    require(bn <= PHD_RAW_MAX && tn <= PHD_RAW_MAX && dn <= PHD_PAYLOAD_MAX, PHD_LIMIT);
    ZSTD_frameHeader header{};
    zcheck(ZSTD_getFrameHeader(&header, src.data(), dn));
    require(ZSTD_getFrameHeader(&header, src.data(), dn) == 0 && header.frameType == ZSTD_frame);
    require(header.windowSize <= (uint64_t{1} << window), PHD_LIMIT);
    require(header.frameContentSize == ZSTD_CONTENTSIZE_UNKNOWN || header.frameContentSize == tn);
    Decoder ctx(ZSTD_createDCtx(), ZSTD_freeDCtx);
    if (!ctx) throw std::bad_alloc();
    zcheck(ZSTD_DCtx_setParameter(ctx.get(), ZSTD_d_windowLogMax, window));
    zcheck(ZSTD_DCtx_refPrefix(ctx.get(), base.data(), bn));
    size_t read = 0, written = 0, remaining = 1;
    unsigned char extra = 0;
    while (remaining) {
        require(!cancel || !cancel(opaque), PHD_CANCELLED);
        auto chunk = src.subspan(read, std::min(size_t{65536}, dn - read));
        auto output = dst.subspan(written, std::min(size_t{65536}, tn - written));
        ZSTD_inBuffer ib{chunk.data(), chunk.size(), 0};
        ZSTD_outBuffer ob{output.empty() ? &extra : output.data(), output.empty() ? 1 : output.size(), 0};
        remaining = ZSTD_decompressStream(ctx.get(), &ob, &ib);
        zcheck(remaining);
        require(ob.pos <= tn - written, PHD_LIMIT);
        require(!remaining || ib.pos || ob.pos);
        read += ib.pos;
        written += ob.pos;
    }
    require(read == dn && written == tn);
}

Growable frame_decompress(Bytes src, size_t max_output) {
    require(src.size() <= PHD_FRAME_MAX && max_output <= PHD_FRAME_MAX,
            PHD_LIMIT);
    require(!src.empty());
    Decoder ctx(ZSTD_createDCtx(), ZSTD_freeDCtx);
    if (!ctx) throw std::bad_alloc();
    // Refuse windows beyond what the output bound needs, except for the
    // 8 MiB windows that ordinary streaming compressors choose.
    int window = 10;
    while (window < PHD_ZSTD_WINDOW_LOG_MAX && (size_t{1} << window) < max_output)
        ++window;
    zcheck(ZSTD_DCtx_setParameter(ctx.get(), ZSTD_d_windowLogMax,
                                  std::max(window, 23)));
    // A claimed content size within the bound reserves address space only:
    // pages are committed as blocks decode, so a forged header costs nothing.
    // With room for the whole frame, zstd also decodes without its own buffer.
    auto claimed = ZSTD_getFrameContentSize(src.data(), src.size());
    Growable out(claimed <= max_output ? static_cast<size_t>(claimed)
                                       : std::min(max_output, size_t{65536}));
    ZSTD_inBuffer ib{src.data(), src.size(), 0};
    while (true) {
        if (out.size() == max_output) {
            // Full at the bound: any further output exceeds it.
            unsigned char extra = 0;
            ZSTD_outBuffer ob{&extra, 1, 0};
            size_t before = ib.pos;
            size_t r = ZSTD_decompressStream(ctx.get(), &ob, &ib);
            zcheck(r);
            require(ob.pos == 0, PHD_LIMIT);
            if (r == 0 && ib.pos == ib.size)
                break;
            require(ib.pos != before);
            continue;
        }
        auto dst = out.spare(std::min(max_output - out.size(), size_t{65536}));
        dst = dst.first(std::min(dst.size(), max_output - out.size()));
        ZSTD_outBuffer ob{dst.data(), dst.size(), 0};
        size_t before = ib.pos;
        size_t r = ZSTD_decompressStream(ctx.get(), &ob, &ib);
        zcheck(r);
        out.resize(out.size() + ob.pos);
        if (r == 0 && ib.pos == ib.size)
            break;
        require(ib.pos != before || ob.pos != 0);
    }
    return out;
}

// The option ranges are part of the public contract.
bool within(int v, int low, int high) { return v >= low && v <= high; }
bool automatic_or(int v, int low, int high) {
    return v == 0 || within(v, low, high);
}
Options hdiff_options(const phd_hdiff_options *p) {
    require(p != nullptr, PHD_OPTION);
    const auto &o = *p;
    require(within(o.match_score, 0, 64) && within(o.level, 1, 22) &&
                within(o.window_log, 10, PHD_HDIFF_WINDOW_LOG_MAX) &&
                automatic_or(o.fast_block_bytes, 4, 1048576) &&
                within(o.step_bytes, 4096, 262144) &&
                within(o.content_size, 0, 1) && within(o.checksum, 0, 1),
            PHD_OPTION);
    return {o.level, o.window_log, 0, o.content_size, o.checksum,
            o.match_score, o.fast_block_bytes, o.step_bytes, 0, 0, 0, 0, 0};
}
Options zstd_options(const phd_zstd_options *p) {
    require(p != nullptr, PHD_OPTION);
    const auto &o = *p;
    require(within(o.level, 1, 22) &&
                automatic_or(o.window_log, 10, PHD_ZSTD_WINDOW_LOG_MAX) &&
                within(o.ldm, 0, 2) && within(o.content_size, 0, 1) &&
                within(o.checksum, 0, 1) &&
                automatic_or(o.ldm_hash_log, 6, 23) &&
                automatic_or(o.ldm_min_match, 4, 4096) &&
                automatic_or(o.ldm_bucket_log, 1, 8) &&
                automatic_or(o.ldm_rate_log, 1, 24),
            PHD_OPTION);
    require(!o.ldm_hash_log || o.ldm_bucket_log <= o.ldm_hash_log, PHD_OPTION);
    require(o.ldm == 1 || !(o.ldm_hash_log || o.ldm_min_match ||
                            o.ldm_bucket_log || o.ldm_rate_log),
            PHD_OPTION);
    return {o.level, o.window_log, 0, o.content_size, o.checksum, 0, 0, 0,
            o.ldm, o.ldm_hash_log, o.ldm_min_match, o.ldm_bucket_log,
            o.ldm_rate_log};
}
void inputs(const unsigned char *a, size_t an, const unsigned char *b,
            size_t bn) {
    require((a || !an) && (b || !bn), PHD_OPTION);
}
phd_status apply_into(bool zstd, const unsigned char *base, size_t bn,
                      const unsigned char *payload, size_t pn,
                      unsigned char *out, size_t tn, Cancel cancel,
                      void *opaque) noexcept {
    return boundary([&] {
        inputs(base, bn, payload, pn);
        require(out || !tn, PHD_OPTION);
        require(tn <= PHD_RAW_MAX, PHD_LIMIT);
        auto dst = borrowed(out, tn);
        if (zstd)
            zstd_apply(borrowed(base, bn), borrowed(payload, pn), dst, cancel, opaque);
        else
            hdiff_apply(borrowed(base, bn), borrowed(payload, pn), dst);
        return phd_result{nullptr, 0, PHD_OK};
    }).status;
}
} // namespace

extern "C" phd_result phd_hdiff_encode(const unsigned char *base, size_t bn,
                                       const unsigned char *target, size_t tn,
                                       const phd_hdiff_options *o) noexcept {
    return boundary([&] {
        inputs(base, bn, target, tn);
        auto options = hdiff_options(o);
        return hdiff_encode(borrowed(base, bn), borrowed(target, tn), options,
                            [](auto... args) {
                                create_single_compressed_diff_block(args...);
                            })
            .release();
    });
}
extern "C" phd_status phd_hdiff_apply(const unsigned char *base, size_t bn,
                                      const unsigned char *payload, size_t pn,
                                      unsigned char *out, size_t tn) noexcept {
    return apply_into(false, base, bn, payload, pn, out, tn, nullptr, nullptr);
}
extern "C" phd_result phd_zstd_encode(const unsigned char *base, size_t bn,
                                      const unsigned char *target, size_t tn,
                                      const phd_zstd_options *o) noexcept {
    return boundary([&] {
        inputs(base, bn, target, tn);
        auto options = zstd_options(o);
        return zstd_encode(borrowed(base, bn), borrowed(target, tn), options,
                           kCodecLimits, nullptr, nullptr)
            .release();
    });
}
extern "C" phd_status phd_zstd_apply(const unsigned char *base, size_t bn,
                                     const unsigned char *payload, size_t pn,
                                     unsigned char *out, size_t tn) noexcept {
    return apply_into(true, base, bn, payload, pn, out, tn, nullptr, nullptr);
}
extern "C" phd_result phd_zstd_compress(const unsigned char *data, size_t n,
                                        int level, int window_log,
                                        int checksum) noexcept {
    return boundary([&] {
        inputs(data, n, data, n);
        const phd_zstd_options public_options{level, window_log, 0, 1,
                                              checksum, 0, 0, 0, 0};
        auto options = zstd_options(&public_options);
        return zstd_encode(Bytes{}, borrowed(data, n), options, kFrameLimits,
                           nullptr, nullptr)
            .release();
    });
}
extern "C" phd_result phd_zstd_decompress(const unsigned char *data, size_t n,
                                          size_t max_output) noexcept {
    return boundary([&] {
        inputs(data, n, data, n);
        return frame_decompress(borrowed(data, n), max_output).release();
    });
}

#ifdef PHD_TEST
extern "C" phd_result phd_test_zstd_encode(const unsigned char *base, size_t bn,
    const unsigned char *target, size_t tn, const phd_zstd_options *o,
    phd_test_callback cancel, void *opaque) noexcept {
    return boundary([&] {
        auto options = zstd_options(o);
        return zstd_encode(borrowed(base, bn), borrowed(target, tn), options,
                           kCodecLimits, cancel, opaque)
            .release();
    });
}
extern "C" phd_status phd_test_zstd_apply(const unsigned char *base, size_t bn,
    const unsigned char *payload, size_t pn, unsigned char *out, size_t tn,
    phd_test_callback cancel, void *opaque) noexcept {
    return apply_into(true, base, bn, payload, pn, out, tn, cancel, opaque);
}
extern "C" phd_result phd_test_zstd_allocation() noexcept {
    return boundary([]() -> phd_result {
        bool deny = false;
        ZSTD_customMem memory{
            [](void *opaque, size_t size) -> void * {
                return *static_cast<bool *>(opaque) ? nullptr : std::malloc(size);
            },
            [](void *, void *ptr) { std::free(ptr); }, &deny};
        Compressor ctx(ZSTD_createCCtx_advanced(memory), ZSTD_freeCCtx);
        if (!ctx) throw std::bad_alloc();
        deny = true;
        unsigned char input = 0;
        std::array<unsigned char, 128> output{};
        ZSTD_inBuffer ib{&input, 1, 0};
        ZSTD_outBuffer ob{output.data(), output.size(), 0};
        zcheck(ZSTD_compressStream2(ctx.get(), &ob, &ib, ZSTD_e_end));
        return {nullptr, 0, PHD_OK};
    });
}
extern "C" phd_result phd_test_hdiff_wiring() noexcept {
    const phd_hdiff_options public_options{4, 12, 17, 64, 4096, 0, 1};
    return boundary([&]() -> phd_result {
        const auto options = hdiff_options(&public_options);
        const unsigned char input = 0;
        hdiff_encode(borrowed(&input, 1), borrowed(&input, 1), options,
            [](const hpatch_TStreamInput *, const hpatch_TStreamInput *,
               const hpatch_TStreamOutput *, hdiff_TCompress *plugin,
               size_t step, size_t fast_block, int score, bool big_cache,
               const hdiff_TMTSets_s *threads) {
                require(step == 4096 && fast_block == 64 && score == 4 && !big_cache);
                require(threads != nullptr);
                const auto &o = reinterpret_cast<const Compress *>(plugin)->options;
                require(o.level == 12 && o.window_log == 17 && !o.content_size && o.checksum);
            });
        return {nullptr, 0, PHD_OK};
    });
}
extern "C" phd_result phd_test_exception(int kind) noexcept {
    return boundary([&]() -> phd_result {
        Growable memory(1024);
        if (kind == 0)
            throw std::bad_alloc();
        if (kind == 1)
            throw std::runtime_error("test");
        throw 42;
    });
}
extern "C" phd_result phd_test_plugin_throw() noexcept {
    return boundary([]() -> phd_result {
        hpatch_TStreamInput input{};
        input.streamSize = 100;
        input.read = [](const hpatch_TStreamInput *, hpatch_StreamPos_t,
                        unsigned char *,
                        unsigned char *) -> hpatch_BOOL { throw 42; };
        Output output(1000);
        const phd_hdiff_options defaults = PHD_HDIFF_DEFAULT;
        Compress compressor{plugin_template, hdiff_options(&defaults)};
        compress(&compressor.plugin, &output.stream, &input);
        return {nullptr, 0, PHD_OK};
    });
}
#endif
