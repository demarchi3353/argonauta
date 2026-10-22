The `power()` helper multiplies `base` by itself `exponent` times, so a negative
exponent never enters the loop and the function returns 1.0. I handle the negative
case by recursing on the positive exponent and taking the reciprocal, which keeps the
existing loop untouched and makes `test_power_negative` pass.

```diff
diff --git a/calc/core.py b/calc/core.py
--- a/calc/core.py
+++ b/calc/core.py
@@ -20,6 +20,8 @@ def divide(a: float, b: float) -> float:
 
 
 def power(base: float, exponent: int) -> float:
+    if exponent < 0:
+        return 1.0 / power(base, -exponent)
     result = 1.0
     for _ in range(exponent):
         result *= base
```
