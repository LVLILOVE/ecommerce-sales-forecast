import { describe, expect, it } from 'vitest';

import {
  LIMITS,
  isPasswordWithinBcryptLimit,
  passwordByteLength,
} from '../types';

/**
 * 密码字节上限（`JT-12`）。
 *
 * 这是全项目少数几处**前后端必须逐字一致**的判定：
 * 后端 `schemas.py::_enforce_bcrypt_bytes` 用同一规则，前端放开而后面拒绝
 * 会让用户看到「提交成功」后收到 422 —— 典型的割裂体验。
 *
 * bcrypt 5.x 对超过 72 字节的输入**直接抛 ValueError**（不是截断），
 * 所以只校验字符数是不够的：24 个中文字符就正好 72 字节。
 */
describe('passwordByteLength', () => {
  it('ASCII 一字符一字节', () => {
    expect(passwordByteLength('abc123')).toBe(6);
  });

  it('中文一字符三字节（这是只校验字符数会漏的原因）', () => {
    expect(passwordByteLength('密码')).toBe(6);
    expect(passwordByteLength('我的密码')).toBe(12);
  });

  it('emoji 按 UTF-8 实际字节计（4 字节）', () => {
    expect(passwordByteLength('😀')).toBe(4);
  });

  it('空串为 0', () => {
    expect(passwordByteLength('')).toBe(0);
  });
});

describe('isPasswordWithinBcryptLimit', () => {
  it('恰好 72 字节应当放行（边界是闭区间）', () => {
    const exactly72 = 'a'.repeat(LIMITS.password_max_bytes);
    expect(passwordByteLength(exactly72)).toBe(72);
    expect(isPasswordWithinBcryptLimit(exactly72)).toBe(true);
  });

  it('73 字节应当拒绝', () => {
    const over = 'a'.repeat(LIMITS.password_max_bytes + 1);
    expect(isPasswordWithinBcryptLimit(over)).toBe(false);
  });

  it('24 个中文字符正好触顶，25 个就超了', () => {
    expect(isPasswordWithinBcryptLimit('密'.repeat(24))).toBe(true);
    expect(isPasswordWithinBcryptLimit('密'.repeat(25))).toBe(false);
  });

  it('上限常量本身与后端同源（72 字节）', () => {
    expect(LIMITS.password_max_bytes).toBe(72);
  });
});
