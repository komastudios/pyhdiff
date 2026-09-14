// A module built against the system libstdc++, loaded beside pyhdiff to check
// that two C++ runtimes coexist: each throws and catches its own exceptions.
#include <stdexcept>
#include <string>
extern "C" __attribute__((visibility("default"))) int std_probe() {
    try {
        throw std::runtime_error(std::string("probe"));
    } catch (const std::exception &e) {
        return std::string(e.what()) == "probe";
    }
}
