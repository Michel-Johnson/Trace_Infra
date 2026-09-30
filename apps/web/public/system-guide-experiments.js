// A deterministic, local simulation for the explanatory page, not a forecast
// service or an optimizer. Each policy sees the same synthetic demand stream.
export const experimentMethods = [
  {
    id: 'fixed-min-max',
    name: '固定上下限补货',
    description: '各店低于 1.2 天基准销量时，按门店顺序补到 2.8 天；受共享预算与发运额度约束。',
  },
  {
    id: 'demand-priority',
    name: '需求优先分配',
    description: '用最近两天的实际需求更新预测，优先给库存覆盖天数较少的门店分配采购额度。',
  },
  {
    id: 'transfer-first',
    name: '先调拨，再采购',
    description: '先把高库存门店的可用库存调给缺货风险门店，再按需求优先分配采购额度。',
  },
];

export const experimentConditions = [
  { id: 'normal', name: '常规需求', description: '各店需求围绕自己的基准销量波动，采购次日到货。' },
  { id: 'local-spike', name: '局部门店需求激增', description: '第 3–5 天，10 家门店需求升至约 1.8 倍；策略事先不知道涨幅。' },
  { id: 'delivery-delay', name: '采购配送延迟', description: '第 2–4 天发出的采购订单延迟一天到货；店间调拨仍可当天完成。' },
];

const STORE_COUNT = 50;
const DAY_COUNT = 7;
const SEED = 20260915;
const DAILY_BUDGET_CENTS = 1320000;
const DAILY_DISPATCH_CAPACITY = 650;
const PURCHASE_CENTS = 2000;
const DELIVERY_CENTS = 200;
const TRANSFER_CENTS = 150;
const HOLDING_CENTS = 20;
const DISPOSAL_CENTS = 50;
const SHELF_LIFE_DAYS = 4;

const sum = (values) => values.reduce((total, value) => total + value, 0);
const stock = (batches) => sum(batches.map((batch) => batch.units));
const money = (cents) => cents / 100;

function seededRandom(seed) {
  let state = seed >>> 0;
  return () => {
    state = (Math.imul(state, 1664525) + 1013904223) >>> 0;
    return state / 4294967296;
  };
}

function makeDataset(conditionId) {
  const random = seededRandom(SEED);
  const stores = Array.from({ length: STORE_COUNT }, (_, index) => {
    const baseline = 8 + Math.floor(random() * 9);
    const initialUnits = Math.round(baseline * (0.4 + random() * 3.8));
    const early = Math.floor(initialUnits / 3);
    return {
      id: index + 1,
      baseline,
      batches: [
        { units: early, expires_on: 2 },
        { units: early, expires_on: 3 },
        { units: initialUnits - early * 2, expires_on: 4 },
      ],
    };
  });
  const demand = Array.from({ length: DAY_COUNT }, (_, day) => stores.map((store, index) => {
    // Draw once per store/day for every condition, so the baseline noise is
    // identical. An unannounced local spike changes only the designated stores.
    const noise = 0.75 + random() * 0.5;
    const spike = conditionId === 'local-spike' && day >= 2 && day <= 4 && index % 5 === 0;
    return Math.max(1, Math.round(store.baseline * noise * (spike ? 1.8 : 1)));
  }));
  return { stores, demand };
}

// Remove the earliest-expiring units first. Keeping cohorts intact through
// transfers prevents a transfer from giving old inventory a new shelf life.
function take(batches, requested) {
  let remaining = requested;
  const taken = [];
  batches.sort((a, b) => a.expires_on - b.expires_on);
  for (const batch of batches) {
    const units = Math.min(batch.units, remaining);
    if (units > 0) {
      batch.units -= units;
      remaining -= units;
      taken.push({ units, expires_on: batch.expires_on });
    }
    if (remaining === 0) break;
  }
  return taken;
}

function runPolicy(method, conditionId, data) {
  const inventories = data.stores.map((store) => store.batches.map((batch) => ({ ...batch })));
  const pipeline = [];
  const costs = { procurement: 0, delivery: 0, transfer: 0, holding: 0, disposal: 0 };
  const daily = [];
  const initialStock = sum(inventories.map(stock));
  const totals = { served: 0, unmet: 0, waste: 0, purchased: 0, transferred: 0 };

  for (let day = 0; day < DAY_COUNT; day += 1) {
    const openingStock = sum(inventories.map(stock));
    let wasted = 0;
    let arrived = 0;
    let transferred = 0;
    let ordered = 0;
    let spent = 0;
    let dispatched = 0;

    for (const inventory of inventories) {
      for (const batch of inventory) {
        if (batch.expires_on <= day) {
          wasted += batch.units;
          batch.units = 0;
        }
      }
    }
    for (const shipment of pipeline) {
      if (shipment.arrival_day === day) {
        inventories[shipment.store_index].push({ units: shipment.units, expires_on: day + SHELF_LIFE_DAYS });
        arrived += shipment.units;
      }
    }

    // All policies observe past demand, including lost sales. No policy reads
    // today's realized demand or the future demand matrix when placing orders.
    const forecast = data.stores.map((store, index) => {
      if (method.id === 'fixed-min-max' || day === 0) return store.baseline;
      const history = data.demand.slice(Math.max(0, day - 2), day).map((row) => row[index]);
      return sum(history) / history.length;
    });
    const pending = data.stores.map((_, index) => sum(pipeline
      .filter((shipment) => shipment.store_index === index && shipment.arrival_day > day)
      .map((shipment) => shipment.units)));

    if (method.id === 'transfer-first') {
      const recipients = data.stores.map((_, index) => index)
        .sort((a, b) => stock(inventories[a]) / forecast[a] - stock(inventories[b]) / forecast[b] || a - b);
      for (const recipient of recipients) {
        let needed = Math.max(0, Math.ceil(forecast[recipient] * 1.2) - stock(inventories[recipient]));
        const donors = data.stores.map((_, index) => index)
          .filter((index) => index !== recipient)
          .sort((a, b) => stock(inventories[b]) / forecast[b] - stock(inventories[a]) / forecast[a] || a - b);
        for (const donor of donors) {
          const surplus = Math.max(0, stock(inventories[donor]) - Math.ceil(forecast[donor] * 1.8));
          const units = Math.min(needed, surplus, DAILY_DISPATCH_CAPACITY - dispatched,
            Math.floor((DAILY_BUDGET_CENTS - spent) / TRANSFER_CENTS));
          if (units <= 0) continue;
          inventories[recipient].push(...take(inventories[donor], units));
          needed -= units;
          transferred += units;
          dispatched += units;
          spent += units * TRANSFER_CENTS;
          costs.transfer += units * TRANSFER_CENTS;
          if (needed === 0) break;
        }
      }
    }

    const leadTime = conditionId === 'delivery-delay' && day >= 1 && day <= 3 ? 2 : 1;
    const needs = data.stores.map((store, index) => {
      const available = stock(inventories[index]) + pending[index];
      if (method.id === 'fixed-min-max') {
        return available < store.baseline * 1.2 ? Math.max(0, Math.ceil(store.baseline * 2.8) - available) : 0;
      }
      // Rolling target: current day plus one day in transit and a small buffer.
      // Policies respond to observed inventory and pending orders, without
      // advance knowledge of the scenario or an automatic lead-time upgrade.
      return Math.max(0, Math.ceil(forecast[index] * 2.2) - available);
    });

    function orderOne(index) {
      if (needs[index] <= 0 || dispatched >= DAILY_DISPATCH_CAPACITY ||
          spent + PURCHASE_CENTS + DELIVERY_CENTS > DAILY_BUDGET_CENTS) return false;
      needs[index] -= 1;
      pending[index] += 1;
      ordered += 1;
      dispatched += 1;
      spent += PURCHASE_CENTS + DELIVERY_CENTS;
      costs.procurement += PURCHASE_CENTS;
      costs.delivery += DELIVERY_CENTS;
      const existing = pipeline.find((shipment) => shipment.store_index === index && shipment.order_day === day);
      if (existing) existing.units += 1;
      else pipeline.push({ store_index: index, units: 1, order_day: day, arrival_day: day + leadTime });
      return true;
    }

    if (method.id === 'fixed-min-max') {
      for (let index = 0; index < STORE_COUNT; index += 1) {
        while (orderOne(index)) { /* Allocate this store's request before the next. */ }
      }
    } else {
      while (dispatched < DAILY_DISPATCH_CAPACITY && spent + PURCHASE_CENTS + DELIVERY_CENTS <= DAILY_BUDGET_CENTS) {
        const candidate = data.stores.map((_, index) => index).filter((index) => needs[index] > 0)
          .sort((a, b) => (stock(inventories[a]) + pending[a]) / forecast[a] -
            (stock(inventories[b]) + pending[b]) / forecast[b] || a - b)[0];
        if (candidate === undefined || !orderOne(candidate)) break;
      }
    }

    let served = 0;
    const demand = sum(data.demand[day]);
    for (let index = 0; index < STORE_COUNT; index += 1) {
      served += stock(take(inventories[index], data.demand[day][index]));
    }
    const endingStock = sum(inventories.map(stock));
    costs.holding += endingStock * HOLDING_CENTS;
    costs.disposal += wasted * DISPOSAL_CENTS;
    totals.served += served;
    totals.unmet += demand - served;
    totals.waste += wasted;
    totals.purchased += ordered;
    totals.transferred += transferred;
    daily.push({
      day: day + 1,
      opening_stock: openingStock,
      demand_units: demand,
      arrived_units: arrived,
      ordered_units: ordered,
      transferred_units: transferred,
      served_units: served,
      stockout_units: demand - served,
      waste_units: wasted,
      ending_stock: endingStock,
      in_transit_units: sum(pipeline.filter((shipment) => shipment.arrival_day > day).map((shipment) => shipment.units)),
      dispatch_units: dispatched,
      budget_spend: money(spent),
    });
  }

  const totalDemand = totals.served + totals.unmet;
  return {
    method_id: method.id,
    method_name: method.name,
    fill_rate: totals.served / totalDemand,
    stockout_units: totals.unmet,
    waste_units: totals.waste,
    total_cost: money(sum(Object.values(costs))),
    budget_violations: daily.filter((row) => row.budget_spend > money(DAILY_BUDGET_CENTS)).length,
    capacity_violations: daily.filter((row) => row.dispatch_units > DAILY_DISPATCH_CAPACITY).length,
    ending_stock: sum(inventories.map(stock)),
    initial_stock: initialStock,
    purchased_units: totals.purchased,
    served_units: totals.served,
    transferred_units: totals.transferred,
    in_transit_units: sum(pipeline.filter((shipment) => shipment.arrival_day >= DAY_COUNT).map((shipment) => shipment.units)),
    cost_breakdown: Object.fromEntries(Object.entries(costs).map(([key, cents]) => [key, money(cents)])),
    daily,
  };
}

export function runReplenishmentExperiment(conditionId = 'normal') {
  const condition = experimentConditions.find((item) => item.id === conditionId);
  if (!condition) throw new RangeError(`Unknown replenishment condition: ${conditionId}`);
  const data = makeDataset(conditionId);
  return {
    condition: { ...condition },
    dataset: {
      stores: STORE_COUNT,
      days: DAY_COUNT,
      seed: SEED,
      sku_count: 1,
      description: '固定种子的合成需求；50 店、7 天、1 个易腐商品。三种规则使用相同初始库存和逐店需求。',
      initial_stock: sum(data.stores.map((store) => stock(store.batches))),
      total_demand: sum(data.demand.flat()),
      demand_by_day: data.demand.map((row) => sum(row)),
    },
    rows: experimentMethods.map((method) => runPolicy(method, conditionId, data)),
    unit: 'CNY',
    budget: money(DAILY_BUDGET_CENTS),
    daily_dispatch_capacity: DAILY_DISPATCH_CAPACITY,
    assumptions: {
      procurement_unit_cost: money(PURCHASE_CENTS),
      delivery_unit_cost: money(DELIVERY_CENTS),
      transfer_unit_cost: money(TRANSFER_CENTS),
      holding_unit_cost_per_day: money(HOLDING_CENTS),
      disposal_unit_cost: money(DISPOSAL_CENTS),
      arrival_shelf_life_days: SHELF_LIFE_DAYS,
      baseline_lead_time_days: 1,
    },
    notes: [
      '浏览器实际计算的合成实验，不调用大模型，不代表真实门店结果或全局最优。满足率 = 已售数量 / 需求数量；缺货需求按流失处理，各策略可观察历史缺货需求。',
      '每日预算共同约束采购、采购配送和店间调拨；每日发运额度同时计入采购发出量和调拨量。采购次日或延后到货，调拨当日完成并保留原保质期。',
      '总成本含新采购、配送、调拨、期末每日库存持有和过期处置；不含初始存货成本和缺货机会成本。第 7 天发出的在途库存单列，成本已计入。',
    ],
  };
}
