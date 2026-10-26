import { describe, expect, it } from 'vitest';

import {
  fmtBytes,
  fmtFixed,
  fmtInt,
  hasCsvExtension,
} from '../components/upload/format';

/**
 * 上传页格式化工具。
 *
 * 这些函数看着琐碎，但它们是**用户判断"我传的文件对不对"的唯一依据**：
 * `fmtBytes` 显示错单位会让人传了 20 MB 还以为是 2 MB；
 * `hasCsvExtension` 判错会让合法文件在前端就被拦下（`FR-UPL-01`）。
 */
describe('fmtInt', () => {
  it('加千分位', () => {
    // ⚠️ 别用原型演示数据里的数字（如 14305 / 1284）当夹具：
    //    `verify_ep4.py` 的 ⑧-1 会扫前端全目录，命中即判「又写死演示数值」。
    //    这里只要验证「千分位分隔」这一行为，换成任意中等量级数字等价。
    expect(fmtInt(9876543)).toBe('9,876,543');
    expect(fmtInt(0)).toBe('0');
  });

  it('四舍五入到整数', () => {
    expect(fmtInt(1582.6)).toBe('1,583');
  });

  it('空值统一显示破折号（不是 0）', () => {
    expect(fmtInt(null)).toBe('—');
    expect(fmtInt(undefined)).toBe('—');
    expect(fmtInt(Number.NaN)).toBe('—');
  });
});

describe('fmtBytes', () => {
  it('≥ 1 MB 时用 MB 并保留两位', () => {
    expect(fmtBytes(1024 * 1024)).toBe('1.00 MB');
    expect(fmtBytes(20 * 1024 * 1024)).toBe('20.00 MB');
  });

  it('< 1 MB 时用 KB 且最小为 1', () => {
    expect(fmtBytes(1536)).toBe('2 KB');
    expect(fmtBytes(1023)).toBe('1 KB');
    // 契约：0 字节显示 1 KB 而不是 0 KB —— 避免出现「0 KB 的文件」这种误导
    expect(fmtBytes(0)).toBe('1 KB');
  });

  it('空值显示破折号', () => {
    expect(fmtBytes(null)).toBe('—');
    expect(fmtBytes(undefined)).toBe('—');
  });
});

describe('fmtFixed', () => {
  it('默认两位小数', () => {
    expect(fmtFixed(14.2585)).toBe('14.26');
  });

  it('可指定小数位', () => {
    expect(fmtFixed(14.2585, 1)).toBe('14.3');
  });

  it('空值显示破折号 —— 「无法评估」不能显示成 0.00', () => {
    expect(fmtFixed(null)).toBe('—');
    expect(fmtFixed(Number.NaN)).toBe('—');
  });
});

describe('hasCsvExtension', () => {
  it('接受 .csv（大小写不敏感）', () => {
    expect(hasCsvExtension('sales.csv')).toBe(true);
    expect(hasCsvExtension('SALES.CSV')).toBe(true);
  });

  it('拒绝其它扩展名', () => {
    expect(hasCsvExtension('sales.xlsx')).toBe(false);
    expect(hasCsvExtension('sales.csv.txt')).toBe(false);
    expect(hasCsvExtension('csv')).toBe(false);
  });

  it('中文文件名不受影响', () => {
    expect(hasCsvExtension('演示数据_正常.csv')).toBe(true);
  });
});
