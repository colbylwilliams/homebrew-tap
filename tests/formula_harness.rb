require "json"

# Exercise the generated DSL without installing Homebrew or downloading a release.
class CurlGitHubPackagesDownloadStrategy; end

class Formula
  class << self
    attr_reader :metadata, :test_body

    def inherited(child)
      child.instance_variable_set(:@metadata, {})
    end

    [:desc, :homepage, :version, :license, :sha256, :depends_on].each do |name|
      define_method(name) { |value| @metadata[name] = value }
    end

    def on_macos
      yield if ENV.fetch("FORMULA_OS") == "darwin"
    end

    def on_linux
      yield if ENV.fetch("FORMULA_OS") == "linux"
    end

    def on_arm
      yield if ENV.fetch("FORMULA_ARCH") == "arm64"
    end

    def on_intel
      yield if ENV.fetch("FORMULA_ARCH") == "amd64"
    end

    def url(value, **options)
      raise "multiple platform URLs selected" if @metadata[:url]
      @metadata[:url] = value
      @metadata[:options] = options
    end

    def test(&block)
      @test_body = block
    end
  end

  attr_reader :installed, :commands

  class Destination
    def initialize(name, records)
      @name, @records = name, records
    end

    def install(file)
      @records << [@name, file]
    end

    def /(file)
      "#{@name}/#{file}"
    end
  end

  def initialize
    @installed = []
    @commands = []
  end

  def bin
    Destination.new("bin", @installed)
  end

  def prefix
    Destination.new("prefix", @installed)
  end

  def system(*args)
    @commands << args
    true
  end
end

load ARGV.fetch(0)
expected = JSON.parse(ARGV.fetch(1))
metadata = Co.metadata
raise "wrong version" unless metadata[:version] == expected.fetch("version")
raise "wrong checksum" unless metadata[:sha256] == expected.fetch("checksum")
raise "wrong URL" unless metadata[:url] == expected.fetch("url")
raise "custom strategy" unless metadata[:options][:using] == CurlGitHubPackagesDownloadStrategy
if ENV.fetch("FORMULA_OS") == "darwin"
  raise "missing macOS minimum" unless metadata[:depends_on] == { macos: :ventura }
end
formula = Co.new
formula.install
raise "wrong installed files" unless formula.installed == [["bin", "co"], ["prefix", "LICENSE"]]
formula.instance_eval(&Co.test_body)
raise "unsafe version test" unless formula.commands == [["bin/co", "--version"]]
