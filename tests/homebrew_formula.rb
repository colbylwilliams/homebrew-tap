require "formulary"

os, arch, path, checksum, archive, destination = ARGV
Homebrew::SimulateSystem.with(os: os.to_sym, arch: arch.to_sym) do
  formula = Formulary.factory(Pathname(path))
  resource = formula.stable.resource
  raise "wrong version" unless formula.version.to_s == "1.2.3"
  raise "wrong checksum" unless resource.checksum.hexdigest == checksum
  raise "wrong strategy" unless resource.download_strategy == CurlGitHubPackagesDownloadStrategy
  raise "wrong URL" unless resource.url.end_with?("/blobs/sha256:#{checksum}")
  strategy = UnpackStrategy.detect(Pathname(archive))
  strategy.extract_nestedly(to: Pathname(destination))
  raise "missing executable" unless (Pathname(destination)/"co").executable?
  raise "missing license" unless (Pathname(destination)/"LICENSE").file?
end
