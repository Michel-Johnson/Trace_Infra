// Fully synthetic guide data: dialogues, timings, counts and evidence are examples.
// These records do not execute a skill, contact a service or represent live research.
export const businessScenarios = [
{
  "id": "system",
  "tab": "系统优化",
  "title": "集团的巡检 Skill，用得怎么样？",
  "question": "物业设施巡检",
  "description": "集团每天巡检耗费大量人力。Skill 可读取部分点位的摄像头截图和传感器，完成远程检查；集团希望看清各项目的使用情况，找到优化机会。",
  "role": "大型物业管理集团 · 设施运营负责人",
  "task": "调用物业设施巡检 Skill，抽取楼宇指定点位的截图，读取传感器，记录检查结果和需要人工复核的项目。",
  "inputs": "集团近 7 天的项目巡检计划、可自动检查点位、Skill 版本、截图与传感器调用，以及人工复核记录。",
  "success": "看覆盖、失败、转人工与重复取数；结合复核结果评估耗时和人力改善。",
  "presets": [
    {
      "label": "取图失败",
      "query": "取图失败"
    },
    {
      "label": "重复取图",
      "query": "重复取图"
    },
    {
      "label": "联合巡检",
      "query": "联合巡检"
    }
  ],
  "defaultTraceId": "property-camera-unavailable",
  "batchSummary": {
    "label": "集团巡检使用情况 · 近 7 天",
    "columns": [
      {
        "key": "project",
        "label": "项目 · 典型轨迹"
      },
      {
        "key": "coverage",
        "label": "调用覆盖"
      },
      {
        "key": "manual",
        "label": "转人工复核"
      },
      {
        "key": "camera",
        "label": "取图失败"
      },
      {
        "key": "repeat",
        "label": "重复取图"
      }
    ],
    "rows": [
      {
        "project": "华南 · 先排查取图失败",
        "coverage": "400 / 500 · 80%",
        "manual": "120 / 400 · 30%",
        "camera": "156 / 520 · 30%",
        "repeat": "8 / 520 · 1.5%",
        "traceId": "property-camera-unavailable"
      },
      {
        "project": "华北 · 覆盖低，重复取图多",
        "coverage": "160 / 400 · 40%",
        "manual": "16 / 160 · 10%",
        "camera": "16 / 320 · 5%",
        "repeat": "160 / 320 · 50%",
        "traceId": "property-repeat-capture"
      },
      {
        "project": "华东 · 对照联合巡检",
        "coverage": "480 / 600 · 80%",
        "manual": "48 / 480 · 10%",
        "camera": "27 / 540 · 5%",
        "repeat": "60 / 540 · 11.1%",
        "traceId": "property-joint-inspection"
      }
    ],
    "note": "合成汇总，均为 Skill v1.4。覆盖 = 已调用任务 / 计划内可自动巡检任务；转人工以已调用任务为分母；取图失败及重复取图以截图请求数为分母，失败不计为重复。点击项目查看代表轨迹，3 条示例并非汇总的完整明细。"
  },
  "summaryNote": "对话、点位、图像引用、计时与统计均为合成演示；未连接真实摄像头、传感器或现场工单。",
  "traces": [
    {
      "id": "property-camera-unavailable",
      "title": "华南 · 取图失败，转人工复核",
      "summary": "摄像头离线，传感器可读；两轮继续完成可远程核查项",
      "tags": [
        "华南项目",
        "物业设施巡检",
        "取图失败",
        "传感器",
        "人工复核",
        "多轮",
        "skill v1.4"
      ],
      "turns": [
        {
          "id": "property-camera-turn-1",
          "query": "用物业设施巡检 Skill 完成华南园区 A 座今天的巡检：抽取水泵房和地下车库截图，读取漏水及设备运行传感器，标出未完成项。",
          "responses": [
            {
              "id": "property-camera-response-1",
              "text": "先读取巡检 Skill，再取得本次计划所需点位的最新截图。",
              "duration_s": 5.2,
              "output_tokens": 128,
              "calls": [
                {
                  "id": "property-camera-skill",
                  "kind": "read",
                  "name": "read_skill",
                  "input": "property-facility-inspection / SKILL.md · v1.4",
                  "output": "按巡检任务读取已接入点位的摄像头截图、传感器读数及采集时间；证据不足的检查项标为未完成，记录人工复核项。",
                  "duration_s": 0.2,
                  "status": "success",
                  "skill": true
                },
                {
                  "id": "property-camera-fetch",
                  "kind": "read",
                  "name": "cameras.get_snapshots",
                  "input": "{\"project\": \"华南园区\", \"task_id\": \"demo-south-0915\", \"points\": [\"pump-room-01\", \"garage-02\"], \"max_age_s\": 300}",
                  "output": "{\"snapshots\": [], \"errors\": [{\"point\": \"pump-room-01\", \"code\": \"CAMERA_OFFLINE\"}, {\"point\": \"garage-02\", \"code\": \"CAMERA_OFFLINE\"}]}",
                  "duration_s": 8.4,
                  "status": "error"
                }
              ]
            },
            {
              "id": "property-camera-response-2",
              "text": "两个点位都没有取得有效截图，图像检查尚未完成。可以继续读取传感器，并将这些点位列入人工复核。",
              "duration_s": 3.8,
              "output_tokens": 86,
              "calls": []
            }
          ]
        },
        {
          "id": "property-camera-turn-2",
          "query": "继续读取可用的传感器，把没查到的图像检查项单独列给现场人员。",
          "responses": [
            {
              "id": "property-camera-response-3",
              "text": "读取同一任务的传感器；保留取图失败状态，不把传感器正常当成图像检查已完成。",
              "duration_s": 5.2,
              "output_tokens": 128,
              "calls": [
                {
                  "id": "property-camera-sensors",
                  "kind": "read",
                  "name": "sensors.read_latest",
                  "input": "{\"project\": \"华南园区\", \"task_id\": \"demo-south-0915\", \"points\": [\"pump-room-01\", \"garage-02\"]}",
                  "output": "{\"sampled_at\": \"2026-09-15T08:00:00+08:00\", \"age_s\": 40, \"readings\": [{\"point\": \"pump-room-01\", \"leak_alarm\": false, \"pump_running\": true}, {\"point\": \"garage-02\", \"leak_alarm\": false}], \"quality\": \"valid\"}",
                  "duration_s": 1.6,
                  "status": "success"
                },
                {
                  "id": "property-camera-report",
                  "kind": "write",
                  "name": "inspection.save_report",
                  "input": "{\"task_id\": \"demo-south-0915\", \"sensor_checks\": \"complete\", \"image_checks\": \"incomplete\", \"manual_review\": [\"pump-room-01\", \"garage-02\"]}",
                  "output": "{\"report_id\": \"demo-south-report\", \"status\": \"partial\", \"manual_review_count\": 2, \"evidence_refs\": [\"property-camera-fetch\", \"property-camera-sensors\"]}",
                  "duration_s": 0.5,
                  "status": "success"
                }
              ]
            },
            {
              "id": "property-camera-response-4",
              "text": "传感器项已记录；水泵房和车库的图像检查仍待完成，已列为 2 项人工复核。",
              "duration_s": 3.3,
              "output_tokens": 74,
              "calls": []
            }
          ]
        }
      ],
      "annotation": {
        "label": "取图失败 · 待人工复核",
        "source": "物业巡检分析插件 · 示例 v2",
        "rule": "截图接口对两个点位返回 CAMERA_OFFLINE；传感器读取成功，报告仍保留 image_checks=incomplete。",
        "expert": "示例标签，尚无真实专家标注。",
        "validation": "调用返回可追溯；未开展真实标注器验证。",
        "evidenceCallId": "property-camera-fetch"
      },
      "finding": "先按项目、点位和错误码统计取图失败。此例支持排查摄像头接入或可用性；要用现场复核结果验证漏检和误报，不能只看调用成功。"
    },
    {
      "id": "property-repeat-capture",
      "title": "华北 · 同一截图被重复拉取",
      "summary": "两次调用返回相同 frame_id，检查轮询间隔是否合理",
      "tags": [
        "华北项目",
        "物业设施巡检",
        "重复取图",
        "摄像头",
        "缓存",
        "skill v1.4"
      ],
      "turns": [
        {
          "id": "property-repeat-turn-1",
          "query": "巡检华北园区地下车库入口，查看摄像头画面及漏水传感器，并保存本次结果。",
          "responses": [
            {
              "id": "property-repeat-response-1",
              "text": "读取巡检说明，并获取车库入口的截图和漏水读数。",
              "duration_s": 5.2,
              "output_tokens": 128,
              "calls": [
                {
                  "id": "property-repeat-skill",
                  "kind": "read",
                  "name": "read_skill",
                  "input": "property-facility-inspection / SKILL.md · v1.4",
                  "output": "按巡检任务读取已接入点位的摄像头截图、传感器读数及采集时间；证据不足的检查项标为未完成，记录人工复核项。",
                  "duration_s": 0.2,
                  "status": "success",
                  "skill": true
                },
                {
                  "id": "property-repeat-frame-1",
                  "kind": "read",
                  "name": "cameras.get_snapshots",
                  "input": "{\"project\": \"华北园区\", \"point\": \"garage-entry\", \"requested_at\": \"2026-09-15T08:00:20+08:00\"}",
                  "output": "{\"frame_id\": \"demo-frame-north-0800\", \"captured_at\": \"2026-09-15T08:00:00+08:00\", \"image_ref\": \"demo:garage-entry-0800\", \"age_s\": 20}",
                  "duration_s": 4.8,
                  "status": "success"
                },
                {
                  "id": "property-repeat-sensors",
                  "kind": "read",
                  "name": "sensors.read_latest",
                  "input": "{\"project\": \"华北园区\", \"point\": \"garage-entry\"}",
                  "output": "{\"sampled_at\": \"2026-09-15T08:00:00+08:00\", \"leak_alarm\": false, \"quality\": \"valid\"}",
                  "duration_s": 1.2,
                  "status": "success"
                }
              ]
            },
            {
              "id": "property-repeat-response-2",
              "text": "再获取一次同一摄像头画面核对。",
              "duration_s": 3.1,
              "output_tokens": 61,
              "calls": [
                {
                  "id": "property-repeat-frame-2",
                  "kind": "read",
                  "name": "cameras.get_snapshots",
                  "input": "{\"project\": \"华北园区\", \"point\": \"garage-entry\", \"requested_at\": \"2026-09-15T08:00:28+08:00\"}",
                  "output": "{\"frame_id\": \"demo-frame-north-0800\", \"captured_at\": \"2026-09-15T08:00:00+08:00\", \"image_ref\": \"demo:garage-entry-0800\", \"age_s\": 28}",
                  "duration_s": 4.6,
                  "status": "success"
                }
              ]
            },
            {
              "id": "property-repeat-response-3",
              "text": "两次取得的是同一帧，未增加新的图像证据。本次记录以这张画面和传感器读数为依据。",
              "duration_s": 4.1,
              "output_tokens": 97,
              "calls": [
                {
                  "id": "property-repeat-report",
                  "kind": "write",
                  "name": "inspection.save_report",
                  "input": "{\"task_id\": \"demo-north-0915\", \"frame_id\": \"demo-frame-north-0800\", \"remote_checks\": \"complete\"}",
                  "output": "{\"report_id\": \"demo-north-report\", \"remote_scope_only\": true, \"duplicate_frame_requests\": 1}",
                  "duration_s": 0.4,
                  "status": "success"
                }
              ]
            }
          ]
        }
      ],
      "annotation": {
        "label": "重复取图候选",
        "source": "物业巡检分析插件 · 示例 v2",
        "rule": "同一任务、同一点位短时间内再次请求截图，返回相同 frame_id 与 captured_at。重复请求是优化候选，是否必要取决于巡检频率要求。",
        "expert": "示例标签，尚无真实专家标注。",
        "validation": "调用返回可追溯；未开展真实标注器验证。",
        "evidenceCallId": "property-repeat-frame-2"
      },
      "finding": "可比较合并请求、帧去重或调整轮询间隔的实验结果，同时核对检查覆盖、新鲜度和人工复核结果；调用次数变少本身不代表优化成功。"
    },
    {
      "id": "property-joint-inspection",
      "title": "华东 · 截图与传感器联合巡检",
      "summary": "一次批量取图配合最新读数，完成已接入点位的远程检查",
      "tags": [
        "华东项目",
        "物业设施巡检",
        "联合巡检",
        "摄像头",
        "传感器",
        "skill v1.4"
      ],
      "turns": [
        {
          "id": "property-joint-turn-1",
          "query": "用物业设施巡检 Skill 检查华东园区 A 座水泵房和空调机房，结合截图与传感器，形成今日远程巡检记录。",
          "responses": [
            {
              "id": "property-joint-response-1",
              "text": "按本次计划批量读取两处截图和传感器，并核对采集时间。",
              "duration_s": 5.2,
              "output_tokens": 128,
              "calls": [
                {
                  "id": "property-joint-skill",
                  "kind": "read",
                  "name": "read_skill",
                  "input": "property-facility-inspection / SKILL.md · v1.4",
                  "output": "按巡检任务读取已接入点位的摄像头截图、传感器读数及采集时间；证据不足的检查项标为未完成，记录人工复核项。",
                  "duration_s": 0.2,
                  "status": "success",
                  "skill": true
                },
                {
                  "id": "property-joint-frames",
                  "kind": "read",
                  "name": "cameras.get_snapshots",
                  "input": "{\"project\": \"华东园区\", \"points\": [\"pump-room-01\", \"hvac-room-01\"], \"max_age_s\": 300}",
                  "output": "{\"snapshots\": [{\"point\": \"pump-room-01\", \"frame_id\": \"demo-east-pump-0800\", \"image_ref\": \"demo:pump-room-0800\"}, {\"point\": \"hvac-room-01\", \"frame_id\": \"demo-east-hvac-0800\", \"image_ref\": \"demo:hvac-room-0800\"}], \"captured_at\": \"2026-09-15T08:00:00+08:00\", \"age_s\": 30}",
                  "duration_s": 3.2,
                  "status": "success"
                },
                {
                  "id": "property-joint-sensors",
                  "kind": "read",
                  "name": "sensors.read_latest",
                  "input": "{\"project\": \"华东园区\", \"points\": [\"pump-room-01\", \"hvac-room-01\"], \"max_age_s\": 300}",
                  "output": "{\"sampled_at\": \"2026-09-15T08:00:00+08:00\", \"age_s\": 35, \"readings\": [{\"point\": \"pump-room-01\", \"leak_alarm\": false, \"pump_running\": true}, {\"point\": \"hvac-room-01\", \"device_alarm\": false, \"running\": true}], \"quality\": \"valid\"}",
                  "duration_s": 1.3,
                  "status": "success"
                }
              ]
            },
            {
              "id": "property-joint-response-2",
              "text": "示例画面未见明显积水，传感器没有返回告警。记录已接入点位的远程检查结果；原计划中的现场操作与设备内部检查继续由人工完成。",
              "duration_s": 5.6,
              "output_tokens": 143,
              "calls": [
                {
                  "id": "property-joint-report",
                  "kind": "write",
                  "name": "inspection.save_report",
                  "input": "{\"task_id\": \"demo-east-0915\", \"image_checks\": \"complete\", \"sensor_checks\": \"complete\", \"evidence_refs\": [\"property-joint-frames\", \"property-joint-sensors\"]}",
                  "output": "{\"report_id\": \"demo-east-report\", \"remote_checks\": \"complete\", \"onsite_scope\": \"unchanged\", \"finding\": \"本次远程检查项未见异常\"}",
                  "duration_s": 0.4,
                  "status": "success"
                }
              ]
            }
          ]
        }
      ],
      "annotation": {
        "label": "远程检查完成",
        "source": "物业巡检分析插件 · 示例 v2",
        "rule": "计划所需截图和传感器均在示例新鲜度范围内，报告引用了两类证据；完成范围仅限本次远程检查项。",
        "expert": "示例标签，尚无真实专家标注。",
        "validation": "调用返回可追溯；未开展真实标注器验证。",
        "evidenceCallId": "property-joint-report"
      },
      "finding": "用成功轨迹作对照：批量取图是否减少请求、是否保留点位覆盖和数据新鲜度。节省的人工时间需结合现场工时另行验证。"
    }
  ]
},
  {
    id: 'geo',
    tab: 'GEO 研究',
    title: '用户规划旅行时，怎样更容易发现我的旅行社？',
    question: '杭州亲子游',
    description: '用同一组旅行规划问题，比较旅行社内容能否被检索、被引用，以及是否满足行程约束。',
    role: '山海旅行社负责人 · 虚构机构',
    task: '从上海出发，安排 2 大 1 小在杭州玩两天；总预算 3,500 元，包含交通和住宿。',
    inputs: '固定的 20 个杭州亲子周末游问题、三种旅行社页面版本，以及每次生成的检索与引用记录。',
    success: '判断哪些内容进入回答，以及引用是否有证据；不把曝光当成预订或收入。',
    presets: [
      { label: '未进入候选', query: '未进入候选' },
      { label: '检索但未引用', query: '检索但未引用' },
      { label: '引用行程依据', query: '引用行程依据' },
    ],
    batchSummary: {
      label: '相同问题集的三组试验 · 合成数据',
      columns: [
        { key: 'variant', label: '旅行社内容版本' },
        { key: 'queries', label: '相同问题数' },
        { key: 'retrieved', label: '进入候选 / 问题数' },
        { key: 'cited', label: '最终引用 / 问题数' },
      ],
      rows: [
        { variant: 'A · 只有品牌介绍', queries: '20', retrieved: '4 / 20 · 20%', cited: '1 / 20 · 5%' },
        { variant: 'B · 有路线，缺少费用边界', queries: '20', retrieved: '12 / 20 · 60%', cited: '5 / 20 · 25%' },
        { variant: 'C · 路线、适龄和费用齐全', queries: '20', retrieved: '17 / 20 · 85%', cited: '11 / 20 · 55%' },
      ],
      note: '每版对相同 20 问各运行一次，合成示例固定其他条件。按问题计数，重复引用只计一次。下面是三个独立试验片段，不是同一用户旅程；这些数值不是实测提升或转化结果。',
    },
    summaryNote: '旅行社、报价、对话和试验数字均为虚构演示；没有真实搜索、广告投放或预订。',
    traces: [
      {
        id: 'geo-brand-only',
        title: 'A · 品牌页没有进入候选',
        summary: '用户需要杭州亲子路线，品牌介绍缺少相关内容',
        tags: ['杭州亲子游', '未进入候选', '版本 A', '独立试验'],
        turns: [
          {
            id: 'geo-brand-turn-1',
            query: '上海出发，2 大 1 小去杭州玩两天，总预算 3,500 元含交通住宿。请安排轻松一点的路线。',
            responses: [
              {
                id: 'geo-brand-response-1',
                text: '检索亲子路线、住宿和交通资料，再核对预算。',
                duration_s: 6.2,
                output_tokens: 143,
                calls: [
                  { id: 'geo-brand-search', kind: 'read', name: 'search.travel_sources', input: '{"query":"上海出发 杭州 两天 亲子 3500 交通 住宿","content_variant":"A"}', output: '{"candidates":["demo:city-family-route","demo:rail-fares","demo:family-hotel"],"agency_page_in_candidates":false}', duration_s: 2.1, status: 'success' },
                  { id: 'geo-brand-read', kind: 'read', name: 'read_sources', input: '["demo:city-family-route","demo:rail-fares","demo:family-hotel"]', output: '演示资料覆盖西湖、儿童活动与住宿费用；候选中没有山海旅行社的页面。', duration_s: 1.4, status: 'success' },
                ],
              },
              { id: 'geo-brand-response-2', text: '建议第一天西湖散步，第二天安排儿童活动；预算按交通、住宿和餐饮分别核算。本次回答引用的是城市路线与交通住宿资料。', duration_s: 5.7, output_tokens: 173, calls: [] },
            ],
          },
        ],
        annotation: {
          label: '未进入检索候选',
          source: '旅行内容可见度标注器 · 示例 v1',
          rule: '在本次已记录的候选列表中找不到目标旅行社页面。',
          expert: '参照标注示例：候选阶段未出现。未提交真实专家标注。',
          validation: '示例材料：完整候选列表与目标页面 ID 对照；未开展真实验证。',
          evidenceCallId: 'geo-brand-search',
        },
        finding: '这条试验中页面没有进入候选。需要检查问题相关性和页面可检索性，不能直接归因为品牌知名度。',
      },
      {
        id: 'geo-vague-offer',
        title: 'B · 找到了路线，但没有引用',
        summary: '报价只写“低至 999”，无法核对全家总预算',
        tags: ['杭州亲子游', '检索但未引用', '版本 B', '独立试验'],
        turns: [
          {
            id: 'geo-vague-turn-1',
            query: '上海出发，2 大 1 小去杭州两天，预算 3,500 元含交通住宿。请给出可核对费用的行程。',
            responses: [
              {
                id: 'geo-vague-response-1',
                text: '查看候选路线的费用范围，确认是否覆盖一家三口的全部行程。',
                duration_s: 5.6,
                output_tokens: 127,
                calls: [
                  { id: 'geo-vague-search', kind: 'read', name: 'search.travel_sources', input: '{"query":"杭州亲子两天 上海出发 交通住宿总价","content_variant":"B"}', output: '{"candidates":["demo:shanhai-route-b","demo:itemized-family-route"],"agency_page_in_candidates":true}', duration_s: 1.7, status: 'success' },
                  { id: 'geo-vague-page', kind: 'read', name: 'read_page', input: 'demo:shanhai-route-b', output: '山海旅行社 · 杭州亲子两日游，低至 999 元。未说明按人还是按团、儿童价、交通住宿是否包含或适用日期。', duration_s: 0.9, status: 'success' },
                  { id: 'geo-vague-alternative', kind: 'read', name: 'read_page', input: 'demo:itemized-family-route', output: '另一份演示资料按 2 大 1 小列出交通、1 晚住宿、活动和餐饮预算，可核对 3,500 元约束。', duration_s: 0.8, status: 'success' },
                ],
              },
              { id: 'geo-vague-response-2', text: '山海页面的 999 元没有费用范围，无法确认是否符合全家预算。这份行程采用可逐项核对的另一份资料，没有引用该报价作为依据。', duration_s: 5.1, output_tokens: 162, calls: [] },
            ],
          },
        ],
        annotation: {
          label: '检索到 · 未引用',
          source: '旅行内容可见度标注器 · 示例 v1',
          rule: '目标页面被读取，但最终行程未引用它；模型响应明确指出价格边界缺失。',
          expert: '参照标注示例：候选已出现，费用证据不足。未提交真实专家标注。',
          validation: '示例材料：页面摘录、模型响应及最终引用对照；未开展真实验证。',
          evidenceCallId: 'geo-vague-page',
        },
        finding: '本次有检索曝光，缺少费用边界使它未被用作行程依据。检索命中与最终引用需要分开统计。',
      },
      {
        id: 'geo-detailed-itinerary',
        title: 'C · 回答引用了路线和费用依据',
        summary: '澄清孩子年龄与出游日期，再读取对应套餐明细',
        tags: ['杭州亲子游', '引用行程依据', '版本 C', '独立试验', '多轮'],
        turns: [
          {
            id: 'geo-detailed-turn-1',
            query: '上海出发，2 大 1 小去杭州玩两天，总预算 3,500 元含交通住宿，想轻松一些。',
            responses: [
              { id: 'geo-detailed-response-1', text: '孩子几岁？计划哪个周末出发？这会影响适合的活动和报价。', duration_s: 3.7, output_tokens: 68, calls: [] },
            ],
          },
          {
            id: 'geo-detailed-turn-2',
            query: '孩子 7 岁，9 月 19 日出发，周日晚上回来。',
            responses: [
              {
                id: 'geo-detailed-response-2',
                text: '按日期和年龄检索，核对往返交通、住宿及活动的费用范围。',
                duration_s: 5.2,
                output_tokens: 118,
                calls: [
                  { id: 'geo-detailed-search', kind: 'read', name: 'search.travel_sources', input: '{"query":"上海 杭州 2026-09-19 两天 7岁 亲子 3500","content_variant":"C"}', output: '{"candidates":["demo:shanhai-route-c","demo:city-family-guide"],"agency_page_in_candidates":true}', duration_s: 1.9, status: 'success' },
                  { id: 'geo-detailed-page', kind: 'read', name: 'read_page', input: 'demo:shanhai-route-c', output: '{"agency":"山海旅行社（虚构）","date":"2026-09-19","party":"2大1小，儿童7岁","days":2,"itinerary":["西湖慢行与湖畔活动","自然探索体验"],"package_yuan":2780,"includes":["往返交通","1晚家庭房","列明活动"],"excludes":["餐饮","个人消费"],"meal_budget_yuan":500,"estimated_total_yuan":3280,"price_is_demo":true}', duration_s: 1.1, status: 'success' },
                ],
              },
              { id: 'geo-detailed-response-3', text: '可以参考山海旅行社这份两日路线：第一天西湖慢行，第二天自然探索。演示页面列明套餐 2,780 元含交通、住宿和活动；另留餐饮 500 元，总计 3,280 元，预算还余 220 元。引用：demo:shanhai-route-c。这是行程资料引用，尚未预订。', duration_s: 6.4, output_tokens: 221, calls: [] },
            ],
          },
        ],
        annotation: {
          label: '引用路线与费用依据',
          source: '旅行内容可见度标注器 · 示例 v1',
          rule: '回答包含目标页面引用，路线、人数、日期和费用可逐项回指到已读取内容。',
          expert: '参照标注示例：引用与页面内容一致。未提交真实专家标注。',
          validation: '示例材料：引用位置、页面摘录与费用核算；未开展真实验证，也没有转化数据。',
          evidenceCallId: 'geo-detailed-page',
        },
        finding: '回答使用了可核对的路线和费用信息。这里只能观察引用曝光，不能推断预订、收入或真实 GEO 提升。',
      },
    ],
  },
];
