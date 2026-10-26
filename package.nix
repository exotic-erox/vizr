{ lib
, python3
, portaudio
, playerctl
, makeWrapper
}:

python3.pkgs.buildPythonApplication {
  pname = "vizr";
  version = "5.1.0";
  format = "pyproject";

  src = ./.;

  build-system = with python3.pkgs; [
    setuptools
    wheel
  ];

  propagatedBuildInputs = with python3.pkgs; [
    numpy
    pillow
    sounddevice
    playerctl
  ];

  buildInputs = [ portaudio ];

  meta = with lib; {
    description = "Terminal particle music visualizer with now-playing HUD";
    homepage = "https://github.com/exotic-erox/vizr";
    license = licenses.mit;
    mainProgram = "vizr";
  };
}
