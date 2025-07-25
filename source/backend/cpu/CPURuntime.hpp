//
//  CPURuntime.hpp
//  MNN
//
//  Created by MNN on 2018/08/31.
//  Copyright © 2018, Alibaba Group Holding Limited
//
#ifndef CPURuntime_hpp
#define CPURuntime_hpp

#include <stdint.h>
#include <vector>
#include "core/Macro.h"
struct CPUGroup {
    uint32_t minFreq;
    uint32_t maxFreq;
    std::vector<int> ids;
};
struct MNNCPUInfo {
    bool fp16arith;
    bool dot;
    bool i8mm;
    bool sve2;
    bool sme2;
    std::vector<CPUGroup> groups;
    int cpuNumber = 0;
};
using cpu_mask_t = unsigned long;
int MNNSetSchedAffinity(const int* cpuIDs, int size);
int MNNGetCurrentPid();
cpu_mask_t MNNGetCPUMask(const std::vector<int>& cpuIds);
const MNNCPUInfo* MNNGetCPUInfo();

#endif /* CPUInfo_hpp */
