/** Runtime validation for the emitted JSON Schema subset; unknown keywords are not interpreted. */
export type Schema = {
  $ref?: string;
  $defs?: Record<string, Schema>;
  anyOf?: Schema[];
  const?: unknown;
  enum?: unknown[];
  type?: string;
  properties?: Record<string, Schema>;
  required?: string[];
  additionalProperties?: boolean | Schema;
  items?: Schema;
  minimum?: number;
  maximum?: number;
  exclusiveMinimum?: number;
  exclusiveMaximum?: number;
  minLength?: number;
  pattern?: string;
  format?: string;
  minItems?: number;
  maxItems?: number;
};

export function validate(
  value: unknown,
  schema: Schema,
  root = schema,
  path = '$',
): void {
  const fail = () => {
    throw new Error(`响应数据不符合契约：${path}`);
  };
  if (schema.$ref) {
    const target = root.$defs?.[schema.$ref.split('/').at(-1)!];
    if (!target) fail();
    return validate(value, target!, root, path);
  }
  if (schema.anyOf) {
    for (const variant of schema.anyOf) {
      try {
        validate(value, variant, root, path);
        return;
      } catch {
        /* try next union branch */
      }
    }
    return fail();
  }
  if ('const' in schema && value !== schema.const) fail();
  if (schema.enum && !schema.enum.includes(value)) fail();
  switch (schema.type) {
    case 'null':
      if (value !== null) fail();
      break;
    case 'boolean':
      if (typeof value !== 'boolean') fail();
      break;
    case 'number':
    case 'integer':
      if (typeof value !== 'number' || !Number.isFinite(value)) return fail();
      if (schema.type === 'integer' && !Number.isInteger(value)) fail();
      if (schema.minimum !== undefined && value < schema.minimum) fail();
      if (schema.maximum !== undefined && value > schema.maximum) fail();
      if (
        schema.exclusiveMinimum !== undefined &&
        value <= schema.exclusiveMinimum
      )
        fail();
      if (
        schema.exclusiveMaximum !== undefined &&
        value >= schema.exclusiveMaximum
      )
        fail();
      break;
    case 'string':
      if (typeof value !== 'string') return fail();
      if (schema.minLength !== undefined && value.length < schema.minLength)
        fail();
      if (schema.pattern && !new RegExp(schema.pattern).test(value)) fail();
      if (
        schema.format === 'date-time' &&
        (!Number.isFinite(Date.parse(value)) ||
          !/(Z|[+-]\d\d:\d\d)$/.test(value))
      )
        fail();
      break;
    case 'array':
      if (!Array.isArray(value)) return fail();
      if (schema.minItems !== undefined && value.length < schema.minItems)
        fail();
      if (schema.maxItems !== undefined && value.length > schema.maxItems)
        fail();
      value.forEach((item, i) =>
        validate(item, schema.items ?? {}, root, `${path}[${i}]`),
      );
      break;
    case 'object': {
      if (value === null || typeof value !== 'object' || Array.isArray(value))
        return fail();
      const row = value as Record<string, unknown>;
      for (const key of schema.required ?? []) if (!(key in row)) fail();
      for (const [key, item] of Object.entries(row)) {
        const child = schema.properties?.[key];
        if (child) validate(item, child, root, `${path}.${key}`);
        else if (schema.additionalProperties === false) fail();
        else if (typeof schema.additionalProperties === 'object')
          validate(item, schema.additionalProperties, root, `${path}.${key}`);
      }
      break;
    }
  }
}
