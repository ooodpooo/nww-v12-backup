#!/usr/bin/env node
/**
 * check-outline-gate.js — 细纲结构硬校验闸门（prealign 确定性落地）
 *
 * 病根：prealign 检查清单写在 SKILL.md 里，是软执行，执行者能选择性绕过 → 缺字段也进写章 → 开篇质量坍塌。
 * 药方：把 prealign 核心结构要求做成确定性脚本，写章入口调用时自动校验，缺必填字段直接 BLOCK，不允许进入 Phase 1。
 *
 * 硬阻断（缺一个都不能进）：
 * - 前三章：E1（章首钩子）+ E2（设定绑定）+ E3（章末钩子）+ E3（认知落差）+ E5（对话双职能）+ E4（阶段目标）+ E0 G7-G8（开篇爽点密度+卖点显性化）
 * - 所有章：出场角色voice锚点 + 本章收益兑现 + 门禁任务卡点 + 门禁冲突推进 + 门禁剧情循环闭环
 *
 * 软提醒（不 BLOCK，只列缺项给执行者）：
 * - 硬判据 A/B/C（细纲容量）：已存在 nww_sequence_audit.py，这里只提醒调用，不重复计算
 * - 配角弧光：仅出场≥3章才检查，跳过
 * - 结构指纹排重：已在 nww_trope_guard.py，这里只提醒调用
 *
 * 用法：
 *   node check-outline-gate.js <细纲路径> [--chapter N]
 *
 * 退出码：
 *   0 = 所有必填字段通过 → 放行
 *   1 = 缺必填字段 → BLOCK，不允许进入写章
 *   2 = 无法读取细纲或格式异常 → BLOCK
 *
 * 兼容规则：
 *   - 旧项目无字段不强制 BLOCK，只输出 ADVISORY → 方便存量逐步迁移
 *   - 开关：`.prealign-gate.enabled` 在书根目录 → 存在则强制 BLOCK 缺项，不存在则仅 advisory
 */

'use strict'
const fs = require('fs')
const path = require('path')

function read(p) {
  try {
    return fs.readFileSync(p, 'utf-8').replace(/^﻿/, '')
  } catch (e) {
    console.error(`ERROR: Cannot read file: ${p}`)
    return null
  }
}

// 必填字段检查清单（按优先级）
const REQUIRED_FIELDS = [
  {
    name: '出场角色voice锚点',
    pattern: /## 出场角色|出场角色\s*voice\s*锚点|### 出场角色/,
    description: '每个出场角色必须标注voice锚点（认知标尺/句法节奏/情绪变调/低频锚点状态）'
  },
  {
    name: '本章目标（统率字段，含收益兑现/章尾钩子；2026-09-09 升级）',
    // 新形态：## 本章目标 统率字段（内含收益兑现/章尾钩子子项）；兼容存量书的旧双字段
    pattern: /## 本章目标[\s\S]{0,4000}?(收益兑现|章尾钩子)|## 本章收益兑现/,
    description: '必须明确本章主爽点/收益兑现/章尾钩子（统率字段或旧双字段二选一）'
  },
  {
    name: 'E1 章首钩子',
    pattern: /E1|章首反常锚点|## 章首钩子/,
    description: '第一幕必须标注章首反常/错位/悬念钩子'
  },
  {
    name: 'E2 设定绑定',
    pattern: /E2|设定绑定/,
    description: '每个新设定必须绑定物/事/价/恐惧，且本章冲突要映照核心命题'
  },
  {
    name: 'E3 章末钩子',
    pattern: /E3|## 章尾钩子|## 章末钩子/,
    description: '章末必须有新疑问/危机/变化（非总结）'
  },
  {
    name: 'E3 认知落差',
    pattern: /认知落差|预期\s*vs\s*实际/,
    description: '必须有一处"主角以为占便宜 → 发现劣势"的认知转折'
  },
  {
    name: 'E5 对话双职能',
    pattern: /E5|对话双职能|这场对话同时办什么事/,
    description: '对话场景必须标注同时承担至少两件职能（露性格/推剧情/藏设定）'
  },
  {
    name: 'E4 阶段目标',
    pattern: /E4|阶段目标/,
    description: '主角必须有明确的本章阶段目标/成长动力'
  },
  {
    name: '门禁·任务卡点',
    pattern: /任务卡点|主角有明显阻碍|## 卡点/,
    description: '本章必须有主角明确的阻碍/选择/代价'
  },
  {
    name: '门禁·冲突推进',
    pattern: /冲突推进|本章有明确冲突/,
    description: '本章必须有明确冲突或冲突升级'
  },
  {
    name: '门禁·剧情循环',
    pattern: /剧情循环|目标→压力→决策→状态变化|## 剧情闭环/,
    description: '细纲必须构成目标→压力→决策→状态变化的闭环（CCv2）'
  }
]

// 2026-10-26 好文章双标尺新增：三层矛盾引擎 + 微观执行边界软检查（ADVISORY，不误伤存量细纲）
const ADVISORY_ENHANCEMENT_FIELDS = [
  {
    name: '多维矛盾引擎·内心价值/代价抉择层',
    pattern: /内心冲突|价值冲突|两难|抉择|代价|取舍|底线|执念/,
    description: '建议在外部冲突与人际关系线之外，明确主角本章的内心价值取舍或代价权衡（三层矛盾引擎）'
  },
  {
    name: '开篇前300字锚定边界',
    pattern: /前\s*300\s*字|前\s*100\s*字|开篇.*锚|首段.*冲突|异常须在/,
    description: '建议明确正文前300字内的反常/冲突/动作抓人边界'
  },
  {
    name: '情感线/群像互动节拍',
    pattern: /情感线|关系线|群像|常客|互动节拍/,
    description: '建议检查是否需要安排情感线或核心群像关系互动节拍（约每2-3章一次推进）'
  }
]

// 前三章额外必填（黄金三章链）
const FIRST_THREE_EXTRA = [
  {
    name: 'G7 开篇爽点密度',
    pattern: /G7|开篇爽点密度|1000字内.*爽点|反常锚点/,
    description: '第1章1000字内必须有爽点/期待点，每章必须有可感知收益'
  },
  {
    name: 'G8 卖点显性化',
    pattern: /G8|卖点.*展示|卖点.*兑现/,
    description: '第1章结束前读者必须能回答"这本书的卖点是什么"'
  },
  {
    name: 'G6 金手指基点闭环',
    pattern: /G6|金手指.*闭环|发现→验证→使用|主角主动使用/,
    description: '前三章内金手指必须完成发现→验证→使用闭环，主角至少一次主动使用'
  }
]

function checkRequired(content, requiredList) {
  const missing = []
  for (const req of requiredList) {
    if (!req.pattern.test(content)) {
      missing.push(req)
    }
  }
  return missing
}

function main() {
  const args = process.argv.slice(2)
  if (args.length === 0) {
    console.error('Usage: node check-outline-gate.js <细纲路径> [--chapter N]')
    process.exit(2)
  }

  const outlinePath = args[0]
  let chapterNum = null
  const chapterArgIdx = args.indexOf('--chapter')
  if (chapterArgIdx !== -1 && args.length > chapterArgIdx + 1) {
    chapterNum = parseInt(args[chapterArgIdx + 1], 10)
  }

  // 判断书根目录（用于开关检测）
  const bookRoot = path.dirname(path.dirname(outlinePath)) // 大纲/细纲_xxx.md → 书根
  const enforceGate = fs.existsSync(path.join(bookRoot, '.prealign-gate.enabled'))

  const content = read(outlinePath)
  if (content === null) {
    process.exit(2)
  }

  // 逐层检查
  let missing = checkRequired(content, REQUIRED_FIELDS)

  // 前三章额外检查
  if (chapterNum && chapterNum <= 3) {
    const missingExtra = checkRequired(content, FIRST_THREE_EXTRA)
    missing = missing.concat(missingExtra)
  }

  const missingAdvisory = checkRequired(content, ADVISORY_ENHANCEMENT_FIELDS)

  // 输出结果
  if (missing.length === 0) {
    console.log('PASS: 细纲结构检查通过，所有必填字段完整')
    if (missingAdvisory.length > 0) {
      console.log(`ADVISORY: 建议关注 ${missingAdvisory.length} 项进阶质量设计（不阻断）：` +
        missingAdvisory.map(x => x.name).join('、'))
    }
    process.exit(0)
  }

  console.log('\n=== check-outline-gate BLOCKING ===')
  console.log(`细纲路径: ${outlinePath}`)
  console.log(`检查模式: ${enforceGate ? '强制 BLOCK（缺项不许进写章）' : 'ADVISORY（仅提醒，旧项目兼容）'}`)
  console.log(`\n缺失 ${missing.length} 项必填结构字段：\n`)
  missing.forEach((item, idx) => {
    console.log(`  ${idx + 1}. ${item.name}: ${item.description}`)
  })
  console.log(`\n根据 prealign 规则，缺项必须补进细纲才能进入写章流程。`)
  console.log('====================================\n')

  // 强制模式 BLOCK，兼容模式仅提醒不阻断
  if (enforceGate) {
    process.exit(1)
  } else {
    console.log('ADVISORY: 项目未启用强制闸门，仅提醒不阻断。建议补全缺项后再写章。')
    process.exit(0)
  }
}

main()
