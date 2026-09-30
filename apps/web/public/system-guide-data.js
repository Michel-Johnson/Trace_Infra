import {businessScenarios} from './system-guide-business-data.js';
import {experimentMethods, experimentConditions} from './system-guide-experiments.js';

const percentage = value => `${(value * 100).toFixed(1)}%`;

// Dialogues illustrate the recording structure. Only experiment results are
// computed; no model or shell is invoked and unknown timings stay unknown.
export function createScienceScenario(report = null, conditionId = 'normal') {
  const condition = experimentConditions.find(item => item.id === conditionId);
  const task = '为 50 家门店统筹一种生鲜商品未来 7 天的补货与调拨。考虑各店库存、需求波动、4 天保质期、共享采购预算和配送额度，比较服务水平与成本。';
  const traces = experimentMethods.map(method => {
    const result = report?.rows.find(row => row.method_id === method.id);
    const prefix = `replenishment-${method.id}-${conditionId}`;
    const evidenceId = `${prefix}-evaluate`;
    const digest = result ? {
      condition:condition.name, method:method.name, fill_rate:percentage(result.fill_rate),
      stockout_units:result.stockout_units, waste_units:result.waste_units,
      total_cost_cny:result.total_cost, ending_stock:result.ending_stock,
      in_transit_units:result.in_transit_units,
      budget_violations:result.budget_violations, capacity_violations:result.capacity_violations,
    } : null;
    return {
      id:`replenishment-${method.id}`, method_id:method.id, title:method.name,
      summary:result ? `满足率 ${percentage(result.fill_rate)} · 缺货 ${result.stockout_units} 件` : method.description,
      tags:[method.name,'门店补货',condition.name],
      turns:[{id:`${prefix}-turn`,query:task,responses:[
        {id:`${prefix}-plan`,text:`采用“${method.name}”：${method.description}`,duration_s:null,output_tokens:null,calls:[
          {id:`${prefix}-read`,kind:'read',name:'read_dataset',input:'demo-stores.json · 固定种子 20260915',output:'50 家门店，7 天，一种生鲜商品。各策略使用相同的初始库存和同一条件下的需求流；只能读取过去的需求。',duration_s:null,status:'success'},
          {id:`${prefix}-write`,kind:'write',name:'select_policy',input:method.id,output:method.description,duration_s:null,status:'success'},
          {id:evidenceId,kind:'bash',name:'evaluate_policy',input:JSON.stringify({method:method.id,condition:conditionId,seed:20260915}),output:result ? JSON.stringify(digest,null,2) : '尚未计算。点击“运行 3 种策略”，此处会显示真实的本地模拟数值。',duration_s:null,status:result?'success':'pending'},
        ]},
        {id:`${prefix}-reply`,text:result ? `${condition.name}下，需求满足率为 ${percentage(result.fill_rate)}，缺货 ${result.stockout_units} 件，报损 ${result.waste_units} 件。费用包括采购、配送、调拨、持有与报损处理；仍需结合期末和在途库存判断。` : '固定相同数据和资源上限，分别记录满足率、缺货、报损与成本，再查看策略差异。',duration_s:null,output_tokens:null,calls:[]},
      ]}],
      annotation:{label:method.name,source:'补货策略规则 · 演示',rule:method.description,
        expert:'尚无专家验证记录。这里按模拟器中的方法定义标注策略，不声称模型能力已经得到验证。',
        validation:result ? `本地规则模拟已运行；满足率＝满足需求件数 / 总需求件数。预算超限 ${result.budget_violations} 次，配送超限 ${result.capacity_violations} 次。模拟器有库存、需求、费用守恒与确定性检查。` : '等待显式运行实验。没有真实模型调用、模型耗时或 token 计量。',evidenceCallId:evidenceId},
      finding:result ? '这条示例连接了方法、条件与模拟结果。可用于理解如何筛选不同的有效策略；模型训练是否改善，需要后续独立实验。' : '先把不同策略在相同数据上的表现放在一起，再定位每种策略对应的过程记录。',
    };
  });
  return {
    id:'science',tab:'模型能力研究',title:'同一组门店，换种补货策略会怎样？',
    role:'研究模型的有效解题策略',question:experimentMethods[0].name,
    description:'比较多种启发式方法在需求变化和配送延迟下的表现，为训练选样提供可追溯的依据。',
    task, inputs:'固定的 50 店 × 7 天合成数据；各店初始库存与需求、共享预算、发运额度和保质期。',
    success:'同一条件下比较需求满足率、缺货、报损和成本，并核对预算、配送与库存守恒。',
    presets:experimentMethods.map(method=>({label:method.name,query:method.name})),traces,
    summaryNote:'对话与工具记录为合成示例；实验数值由本页实际计算。未调用模型，模型耗时与 token 为未知。',
  };
}
export const scenarios = [createScienceScenario(), ...businessScenarios];
