#ifndef SWARMIO_PATTERN_H
#define SWARMIO_PATTERN_H
#ifdef __CUDACC__
__host__ __device__
#endif
static inline unsigned long long swarmio_pattern(unsigned long long word)
{
    unsigned long long x = word + 0x9e3779b97f4a7c15ULL;
    x = (x ^ (x >> 30)) * 0xbf58476d1ce4e5b9ULL;
    x = (x ^ (x >> 27)) * 0x94d049bb133111ebULL;
    return x ^ (x >> 31);
}
#endif
