// Native bridge checks: round trips, every truncation, byte mutations after the
// decoder opens, option wiring, and exception containment at the C boundary.
#include "bridge.h"
#include <cstdlib>
#include <cstring>
#include <iostream>
#include <random>
#include <stdexcept>
#include <vector>
using Bytes = std::vector<unsigned char>;
static void check(bool ok, const char *what) {
    if (!ok) {
        std::cerr << "check failed: " << what << '\n';
        std::abort();
    }
}
static Bytes take(phd_result r, const char *what) {
    if (r.status != PHD_OK)
        std::cerr << what << " status " << r.status << '\n';
    check(r.status == PHD_OK && (r.data || !r.size), what);
    Bytes out(r.data, r.data + r.size);
    std::free(r.data);
    return out;
}
static void failed(phd_result r, const char *what) {
    check(r.status != PHD_OK && !r.data && !r.size, what);
}
using Apply = phd_result (*)(const unsigned char *, size_t, const unsigned char *,
                             size_t, size_t);
static void corrupt(const Bytes &a, const Bytes &b, const Bytes &d, Apply apply) {
    // Every truncation must fail, including compressed and raw headers.
    for (size_t n = 0; n < d.size(); ++n)
        failed(apply(a.data(), a.size(), d.data(), n, b.size()), "truncation");
    // Mutated payloads decode to some bytes or fail cleanly, never crash.
    Bytes mutated = d;
    for (size_t offset = 0; offset < mutated.size(); ++offset) {
        mutated[offset] ^= 0x80;
        auto r = apply(a.data(), a.size(), mutated.data(), mutated.size(), b.size());
        if (r.status == PHD_OK)
            check(r.size == b.size(), "mutation size");
        else
            check(!r.data, "mutation failure owns nothing");
        std::free(r.data);
        mutated[offset] ^= 0x80;
    }
    if (!b.empty())
        failed(apply(a.data(), a.size(), d.data(), d.size(), b.size() - 1), "short target");
    failed(apply(a.data(), a.size(), d.data(), d.size(), b.size() + 1), "long target");
}
static void hdiff_roundtrip(const Bytes &a, const Bytes &b) {
    const phd_hdiff_options options = PHD_HDIFF_DEFAULT;
    auto d = take(phd_hdiff_encode(a.data(), a.size(), b.data(), b.size(), &options), "hdiff encode");
    check(take(phd_hdiff_apply(a.data(), a.size(), d.data(), d.size(), b.size()), "hdiff apply") == b,
          "hdiff roundtrip");
    corrupt(a, b, d, phd_hdiff_apply);
}
static int cancel_now(void *) { return 1; }
static int throw_from_callback(void *kind) {
    if (*static_cast<int *>(kind) == 0) throw std::bad_alloc();
    if (*static_cast<int *>(kind) == 1) throw std::runtime_error("injected");
    throw 42;
}
static void zstd_tests() {
    Bytes base(131072, 'a'), target(131072, 'b');
    phd_hdiff_options h = PHD_HDIFF_DEFAULT;
    h.checksum = 1;
    for (int level : {1, 22}) {
        h.level = level;
        h.window_log = 10;
        h.match_score = level == 1 ? 0 : 64;
        h.fast_block_bytes = level == 1 ? 0 : 1048576;
        h.step_bytes = level == 1 ? 4096 : 262144;
        auto d = take(phd_hdiff_encode(base.data(), base.size(), target.data(), target.size(), &h), "hdiff bounds");
        check(take(phd_hdiff_apply(base.data(), base.size(), d.data(), d.size(), target.size()), "hdiff bounds apply") == target,
              "hdiff bounds roundtrip");
    }
    phd_zstd_options o = PHD_ZSTD_DEFAULT;
    o.level = 19;
    o.window_log = 29;
    o.ldm = 2;
    take(phd_zstd_encode(base.data(), base.size(), target.data(), target.size(), &o), "ldm off");
    o.window_log = 10;
    o.ldm = 1;
    o.ldm_hash_log = 6;
    o.ldm_min_match = 4;
    o.ldm_bucket_log = 1;
    o.ldm_rate_log = 1;
    take(phd_zstd_encode(base.data(), base.size(), target.data(), target.size(), &o), "ldm tuned");
    o.ldm_hash_log = 0;
    o.ldm_min_match = 0;
    o.ldm_bucket_log = 8;
    o.ldm_rate_log = 0;
    take(phd_zstd_encode(base.data(), 2, target.data(), 2, &o), "ldm bucket small");
    o = PHD_ZSTD_DEFAULT;
    o.level = 19;
    for (int content : {0, 1}) {
        o.content_size = content;
        auto d = take(phd_zstd_encode(base.data(), base.size(), target.data(), target.size(), &o), "zstd encode");
        check(take(phd_zstd_apply(base.data(), base.size(), d.data(), d.size(), target.size()), "zstd apply") == target,
              "zstd roundtrip");
        corrupt(base, target, d, phd_zstd_apply);
        auto trailing = d;
        trailing.push_back(0);
        failed(phd_zstd_apply(base.data(), base.size(), trailing.data(), trailing.size(), target.size()), "trailing byte");
        auto twice = d;
        twice.insert(twice.end(), d.begin(), d.end());
        failed(phd_zstd_apply(base.data(), base.size(), twice.data(), twice.size(), target.size()), "second frame");
        failed(phd_test_zstd_apply(base.data(), base.size(), d.data(), d.size(), target.size(), cancel_now, nullptr),
               "apply callback");
        check(phd_test_zstd_encode(base.data(), base.size(), target.data(), target.size(), &o, cancel_now, nullptr).status ==
                  PHD_CANCELLED, "encode callback");
        for (int kind = 0; kind < 3; ++kind) {
            auto expected = kind == 0 ? PHD_ALLOC : kind == 1 ? PHD_EXCEPTION : PHD_UNKNOWN;
            auto r = phd_test_zstd_encode(base.data(), base.size(), target.data(), target.size(), &o,
                                          throw_from_callback, &kind);
            check(r.status == expected && !r.data, "encode exception");
            r = phd_test_zstd_apply(base.data(), base.size(), d.data(), d.size(), target.size(),
                                    throw_from_callback, &kind);
            check(r.status == expected && !r.data, "apply exception");
        }
    }
}
static void option_tests() {
    const unsigned char x = 'x';
    const phd_hdiff_options good_h = PHD_HDIFF_DEFAULT;
    const phd_zstd_options good_z = PHD_ZSTD_DEFAULT;
    auto h_rejects = [&](void (*edit)(phd_hdiff_options &)) {
        auto o = good_h;
        edit(o);
        check(phd_hdiff_encode(&x, 1, &x, 1, &o).status == PHD_OPTION, "hdiff option");
    };
    h_rejects([](phd_hdiff_options &o) { o.match_score = 65; });
    h_rejects([](phd_hdiff_options &o) { o.level = 0; });
    h_rejects([](phd_hdiff_options &o) { o.window_log = 24; });
    h_rejects([](phd_hdiff_options &o) { o.fast_block_bytes = 3; });
    h_rejects([](phd_hdiff_options &o) { o.step_bytes = 4095; });
    h_rejects([](phd_hdiff_options &o) { o.checksum = 2; });
    auto z_rejects = [&](void (*edit)(phd_zstd_options &)) {
        auto o = good_z;
        edit(o);
        check(phd_zstd_encode(&x, 1, &x, 1, &o).status == PHD_OPTION, "zstd option");
    };
    z_rejects([](phd_zstd_options &o) { o.level = 23; });
    z_rejects([](phd_zstd_options &o) { o.window_log = 31; });
    z_rejects([](phd_zstd_options &o) { o.ldm = 3; });
    z_rejects([](phd_zstd_options &o) { o.ldm_hash_log = 6; });
    z_rejects([](phd_zstd_options &o) { o.ldm = 1; o.ldm_hash_log = 6; o.ldm_bucket_log = 7; });
    check(phd_hdiff_encode(&x, 1, &x, 1, nullptr).status == PHD_OPTION, "null options");
    check(phd_hdiff_encode(nullptr, 1, &x, 1, &good_h).status == PHD_OPTION, "null input");
    check(phd_hdiff_encode(nullptr, PHD_RAW_MAX + 1, nullptr, 0, &good_h).status != PHD_OK, "raw limit");
    check(phd_hdiff_apply(&x, 1, &x, 1, PHD_RAW_MAX + 1).status == PHD_LIMIT, "target limit");
    check(phd_zstd_compress(&x, 1, 0, 0, 1).status == PHD_OPTION, "compress level");
}
static void frame_tests() {
    Bytes data(300000);
    std::mt19937 rng(5);
    for (size_t i = 0; i < data.size(); ++i)
        data[i] = static_cast<unsigned char>(i % 251 < 200 ? 'q' : rng());
    for (int checksum : {0, 1}) {
        auto frame = take(phd_zstd_compress(data.data(), data.size(), 3, 0, checksum), "compress");
        check(take(phd_zstd_decompress(frame.data(), frame.size(), data.size()), "decompress") == data,
              "frame roundtrip");
        check(phd_zstd_decompress(frame.data(), frame.size(), data.size() - 1).status == PHD_LIMIT,
              "frame bound");
        for (size_t n = 0; n < frame.size(); n += 1 + n / 16)
            failed(phd_zstd_decompress(frame.data(), n, data.size()), "frame truncation");
        auto twice = frame;
        twice.insert(twice.end(), frame.begin(), frame.end());
        auto both = take(phd_zstd_decompress(twice.data(), twice.size(), 2 * data.size()), "two frames");
        check(both.size() == 2 * data.size(), "two frames size");
        check(phd_zstd_decompress(twice.data(), twice.size(), 2 * data.size() - 1).status == PHD_LIMIT,
              "two frames bound");
        // A skippable frame between data frames carries no output.
        Bytes skippable{0x50, 0x2a, 0x4d, 0x18, 3, 0, 0, 0, 1, 2, 3};
        auto mixed = frame;
        mixed.insert(mixed.end(), skippable.begin(), skippable.end());
        check(take(phd_zstd_decompress(mixed.data(), mixed.size(), data.size()), "skippable") == data,
              "skippable frame");
        auto trailing = frame;
        trailing.push_back(0);
        failed(phd_zstd_decompress(trailing.data(), trailing.size(), data.size()), "trailing garbage");
    }
    auto empty = take(phd_zstd_compress(nullptr, 0, 3, 0, 1), "compress empty");
    check(take(phd_zstd_decompress(empty.data(), empty.size(), 0), "decompress empty").empty(), "empty frame");
    failed(phd_zstd_decompress(nullptr, 0, 10), "no input");
}
int main() {
    check(phd_test_zstd_allocation().status == PHD_ALLOC, "zstd allocation");
    check(phd_test_hdiff_wiring().status == PHD_OK, "hdiff wiring");
    option_tests();
    frame_tests();
    zstd_tests();
    hdiff_roundtrip({}, {});
    hdiff_roundtrip({}, {'a', 'b'});
    hdiff_roundtrip({'a', 'b'}, {});
    Bytes a(32768, 'a'), b = a;
    for (size_t i = 0; i < b.size(); i += 51)
        b[i] = static_cast<unsigned char>(i);
    hdiff_roundtrip(a, b);
    hdiff_roundtrip(a, a);
    std::mt19937 rng(17);
    for (auto &c : b)
        c = static_cast<unsigned char>(rng());
    // Keep the truncation corpus small for random, uncompressed patches.
    b.resize(512);
    hdiff_roundtrip({}, b);
    for (int repeat = 0; repeat < 100; ++repeat) {
        for (int k = 0; k < 3; ++k) {
            auto r = phd_test_exception(k);
            check(!r.data && r.status == (k == 0 ? PHD_ALLOC : k == 1 ? PHD_EXCEPTION : PHD_UNKNOWN),
                  "exception boundary");
        }
        check(phd_test_plugin_throw().status == PHD_UNKNOWN, "plugin throw");
    }
    std::cout << "native tests passed\n";
}
