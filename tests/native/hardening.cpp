#include <array>
#include <cstdlib>
#include <memory>
#include <span>
#include <string>
#include <string_view>
#include <vector>
int main(int argc, char **argv) {
    int kind = argc > 1 ? std::atoi(argv[1]) : 0;
    std::vector<int> v(1);
    std::string s = "a";
    std::array<int, 1> a{};
    std::span<int> p(a);
    std::string_view sv(s);
    auto u = std::make_unique<int[]>(1);
    volatile int value = 0;
    switch (kind) {
    case 0:
        value = v[1];
        break;
    case 1:
        value = *(v.begin() + 1);
        break;
    case 2:
        value = *(s.begin() + 2);
        break;
    case 3:
        value = *(a.begin() + 1);
        break;
    case 4:
        value = *(p.begin() + 1);
        break;
    case 5:
        value = *(sv.begin() + 1);
        break;
    case 6:
        value = u[1];
        break;
    }
    return value;
}
