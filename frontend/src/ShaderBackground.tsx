import { ShaderGradient, ShaderGradientCanvas } from "@shadergradient/react";

export default function ShaderBackground() {
  return (
    <ShaderGradientCanvas
      className="shader-background"
      style={{ position: "fixed", inset: 0, zIndex: 0, pointerEvents: "none" }}
      pixelDensity={0.55}
      fov={45}
      powerPreference="low-power"
      lazyLoad
      rootMargin="160px"
    >
      <ShaderGradient
        type="plane"
        animate="on"
        uSpeed={0.1}
        uStrength={0.7}
        uDensity={0.8}
        uFrequency={0.7}
        color1="#27233f"
        color2="#34304e"
        color3="#1d2138"
        lightType="3d"
        brightness={0.55}
        cDistance={3.6}
        cPolarAngle={90}
        cAzimuthAngle={180}
        grain="off"
        reflection={0}
      />
    </ShaderGradientCanvas>
  );
}
